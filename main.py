import asyncio
import random
import time
from collections import Counter
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# =========================================================
# APP
# =========================================================

app = FastAPI(title="Telegram Poker Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# SETTINGS
# =========================================================

MAX_SEATS = 6
STARTING_CHIPS = 1000
SMALL_BLIND = 10
BIG_BLIND = 20

TURN_SECONDS = 25

RANKS = "23456789TJQKA"
SUITS = ["S", "H", "D", "C"]

SUIT_SYMBOLS = {
    "S": "♠",
    "H": "♥",
    "D": "♦",
    "C": "♣",
}

RANK_VALUES = {
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "T": 10,
    "J": 11,
    "Q": 12,
    "K": 13,
    "A": 14,
}


# =========================================================
# MODELS
# =========================================================

class JoinRequest(BaseModel):
    user_id: str
    name: str = "Player"


class ActionRequest(BaseModel):
    user_id: str
    action: str
    amount: int = 0


class SitRequest(BaseModel):
    user_id: str
    name: str = "Player"
    seat: Optional[int] = None


class ChatRequest(BaseModel):
    user_id: str
    message: str


# =========================================================
# GAME STATE
# =========================================================

players: Dict[str, dict] = {}
websockets: Dict[str, WebSocket] = {}

game = {
    "table_id": 1,
    "started": False,
    "hand_number": 0,
    "stage": "waiting",
    "deck": [],
    "community_cards": [],
    "pot": 0,
    "current_bet": 0,
    "current_player": None,
    "dealer_seat": 0,
    "small_blind_seat": None,
    "big_blind_seat": None,
    "winner": None,
    "message": "Waiting for players",
    "last_action": "",
    "turn_started_at": None,
    "turn_deadline": None,
    "chat": [],
}


# =========================================================
# HELPERS
# =========================================================

def make_deck():
    return [r + s for r in RANKS for s in SUITS]


def card_text(card: str):
    if len(card) < 2:
        return card

    rank = card[:-1]
    suit = card[-1]

    return rank + SUIT_SYMBOLS.get(suit, suit)


def visible_player(p: dict):
    return {
        "user_id": p["user_id"],
        "name": p["name"],
        "chips": p["chips"],
        "seat": p["seat"],
        "bet": p["bet"],
        "total_bet": p["total_bet"],
        "folded": p["folded"],
        "all_in": p["all_in"],
        "is_bot": p["is_bot"],
        "is_turn": game["current_player"] == p["user_id"],
        "cards_count": len(p["cards"]),
    }


def reset_player_for_hand(p: dict):
    p["cards"] = []
    p["folded"] = False
    p["all_in"] = False
    p["bet"] = 0
    p["total_bet"] = 0


def seated_players():
    return sorted(
        [p for p in players.values() if p.get("seat") is not None],
        key=lambda x: x["seat"]
    )


def active_players():
    return [
        p for p in seated_players()
        if not p["folded"]
    ]


def actionable_players():
    return [
        p for p in active_players()
        if not p["all_in"] and p["chips"] > 0
    ]


def player_by_seat(seat: int):
    for p in players.values():
        if p.get("seat") == seat:
            return p
    return None


def find_player(user_id: str):
    return players.get(str(user_id))


def next_seat(start_seat: int):
    for i in range(1, MAX_SEATS + 1):
        seat = (start_seat + i) % MAX_SEATS
        p = player_by_seat(seat)
        if p:
            return seat

    return None


def next_actionable_from_seat(start_seat: int):
    for i in range(1, MAX_SEATS + 1):
        seat = (start_seat + i) % MAX_SEATS
        p = player_by_seat(seat)

        if p and not p["folded"] and not p["all_in"] and p["chips"] > 0:
            return p

    return None


def set_turn(p: Optional[dict]):
    if p is None:
        game["current_player"] = None
        game["turn_started_at"] = None
        game["turn_deadline"] = None
        return

    game["current_player"] = p["user_id"]

    now = time.time()

    game["turn_started_at"] = now
    game["turn_deadline"] = now + TURN_SECONDS


def calculate_pot():
    return sum(p["total_bet"] for p in seated_players())


def current_max_bet():
    return max([p["bet"] for p in seated_players()] + [0])


def everyone_acted_or_allin():
    active = active_players()

    if len(active) <= 1:
        return True

    for p in active:
        if not p["all_in"] and p["bet"] != game["current_bet"]:
            return False

    return True


def normalize_action(action: str):
    return action.lower().strip().replace("-", "").replace("_", "")


# =========================================================
# HAND EVALUATION
# =========================================================

def evaluate_five(cards: List[str]):
    values = sorted(
        [RANK_VALUES[c[:-1]] for c in cards],
        reverse=True
    )

    counts = Counter(values)

    unique_values = sorted(set(values), reverse=True)

    straight_high = None

    if 14 in unique_values:
        unique_values.append(1)

    for i in range(len(unique_values) - 4):
        group = unique_values[i:i + 5]

        if group[0] - group[4] == 4:
            straight_high = group[0]
            break

    flush = len(set(c[-1] for c in cards)) == 1

    if flush and straight_high:
        return (8, straight_high)

    count_groups = sorted(
        counts.items(),
        key=lambda x: (x[1], x[0]),
        reverse=True
    )

    quads = [v for v, c in count_groups if c == 4]

    if quads:
        four = max(quads)
        kicker = max(v for v in values if v != four)
        return (7, four, kicker)

    trips = sorted(
        [v for v, c in counts.items() if c == 3],
        reverse=True
    )

    pairs = sorted(
        [v for v, c in counts.items() if c >= 2],
        reverse=True
    )

    if trips and len(pairs) >= 2:
        trip = trips[0]
        pair = max(v for v in pairs if v != trip)
        return (6, trip, pair)

    if flush:
        return (5, *values)

    if straight_high:
        return (4, straight_high)

    if trips:
        trip = trips[0]
        kickers = sorted(
            [v for v in values if v != trip],
            reverse=True
        )[:2]

        return (3, trip, *kickers)

    pair_values = sorted(
        [v for v, c in counts.items() if c == 2],
        reverse=True
    )

    if len(pair_values) >= 2:
        high_pair = pair_values[0]
        low_pair = pair_values[1]

        kicker = max(
            v for v in values
            if v != high_pair and v != low_pair
        )

        return (2, high_pair, low_pair, kicker)

    if len(pair_values) == 1:
        pair = pair_values[0]

        kickers = sorted(
            [v for v in values if v != pair],
            reverse=True
        )[:3]

        return (1, pair, *kickers)

    return (0, *values)


def combinations(cards, size):
    if len(cards) < size:
        return []

    result = []

    def rec(start, current):
        if len(current) == size:
            result.append(current.copy())
            return

        for i in range(start, len(cards)):
            current.append(cards[i])
            rec(i + 1, current)
            current.pop()

    rec(0, [])
    return result


def evaluate_hand(cards: List[str]):
    best = None

    for five in combinations(cards, 5):
        score = evaluate_five(five)

        if best is None or score > best:
            best = score

    return best or (0,)


def hand_name(score):
    names = {
        8: "Straight Flush",
        7: "Four of a Kind",
        6: "Full House",
        5: "Flush",
        4: "Straight",
        3: "Three of a Kind",
        2: "Two Pair",
        1: "One Pair",
        0: "High Card",
    }

    return names.get(score[0], "High Card")


# =========================================================
# BOT
# =========================================================

async def bot_turn():
    await asyncio.sleep(random.uniform(0.8, 1.8))

    uid = game["current_player"]

    if not uid:
        return

    p = find_player(uid)

    if not p or not p["is_bot"]:
        return

    if game["stage"] == "waiting":
        return

    score = evaluate_hand(p["cards"] + game["community_cards"])

    strength = score[0]

    call_amount = max(0, game["current_bet"] - p["bet"])

    roll = random.random()

    if strength >= 4:
        if p["chips"] <= call_amount:
            action = "allin"
            amount = 0
        else:
            action = "raise"
            amount = max(
                BIG_BLIND * 2,
                game["current_bet"] + BIG_BLIND
            )

    elif strength >= 2:
        if call_amount == 0:
            action = "check"
            amount = 0
        elif roll < 0.8:
            action = "call"
            amount = 0
        else:
            action = "fold"
            amount = 0

    else:
        if call_amount == 0:
            action = "check"
            amount = 0
        elif roll < 0.55:
            action = "call"
            amount = 0
        else:
            action = "fold"
            amount = 0

    try:
        await process_action(uid, action, amount)
    except Exception:
        pass


# =========================================================
# BROADCAST
# =========================================================

def public_game_state(for_user: Optional[str] = None):
    result_players = []

    for p in seated_players():
        item = visible_player(p)

        if for_user and p["user_id"] == str(for_user):
            item["cards"] = [
                card_text(c) for c in p["cards"]
            ]
        else:
            item["cards"] = []

        result_players.append(item)

    return {
        "success": True,
        "table_id": game["table_id"],
        "started": game["started"],
        "hand_number": game["hand_number"],
        "stage": game["stage"],
        "community_cards": [
            card_text(c)
            for c in game["community_cards"]
        ],
        "pot": calculate_pot(),
        "current_bet": game["current_bet"],
        "current_player": game["current_player"],
        "current_player_name": (
            find_player(game["current_player"])["name"]
            if find_player(game["current_player"])
            else None
        ),
        "dealer_seat": game["dealer_seat"],
        "small_blind_seat": game["small_blind_seat"],
        "big_blind_seat": game["big_blind_seat"],
        "winner": game["winner"],
        "message": game["message"],
        "last_action": game["last_action"],
        "turn_deadline": game["turn_deadline"],
        "players": result_players,
        "chat": game["chat"][-30:],
    }


async def broadcast():
    disconnected = []

    for uid, ws in list(websockets.items()):
        try:
            await ws.send_json(public_game_state(uid))
        except Exception:
            disconnected.append(uid)

    for uid in disconnected:
        websockets.pop(uid, None)


# =========================================================
# GAME FLOW
# =========================================================

def burn_card():
    if game["deck"]:
        game["deck"].pop()


def deal_one():
    if not game["deck"]:
        raise RuntimeError("Deck is empty")

    return game["deck"].pop()


def deal_private_cards():
    ps = seated_players()

    for _ in range(2):
        for p in ps:
            p["cards"].append(deal_one())


def collect_blinds():
    ps = seated_players()

    if len(ps) < 2:
        return

    dealer = player_by_seat(game["dealer_seat"])

    if dealer is None:
        game["dealer_seat"] = ps[0]["seat"]

    sb = next_seat(game["dealer_seat"])
    bb = next_seat(sb)

    game["small_blind_seat"] = sb
    game["big_blind_seat"] = bb

    small = player_by_seat(sb)
    big = player_by_seat(bb)

    if small:
        amount = min(SMALL_BLIND, small["chips"])

        small["chips"] -= amount
        small["bet"] += amount
        small["total_bet"] += amount

        if small["chips"] == 0:
            small["all_in"] = True

    if big:
        amount = min(BIG_BLIND, big["chips"])

        big["chips"] -= amount
        big["bet"] += amount
        big["total_bet"] += amount

        if big["chips"] == 0:
            big["all_in"] = True

    game["current_bet"] = BIG_BLIND


def start_new_hand():
    ps = seated_players()

    if len(ps) < 2:
        game["started"] = False
        game["stage"] = "waiting"
        game["message"] = "Waiting for at least 2 players"
        return

    game["hand_number"] += 1
    game["started"] = True
    game["stage"] = "preflop"
    game["deck"] = make_deck()
    random.shuffle(game["deck"])
    game["community_cards"] = []
    game["pot"] = 0
    game["current_bet"] = 0
    game["winner"] = None
    game["last_action"] = ""
    game["message"] = "New hand started"

    for p in ps:
        reset_player_for_hand(p)

    if game["dealer_seat"] not in [p["seat"] for p in ps]:
        game["dealer_seat"] = ps[0]["seat"]
    else:
        nxt = next_seat(game["dealer_seat"])

        if nxt is not None:
            game["dealer_seat"] = nxt

    deal_private_cards()
    collect_blinds()

    bb = player_by_seat(game["big_blind_seat"])

    first = None

    if bb:
        first = next_actionable_from_seat(bb["seat"])

    if first is None:
        first = next(
            (
                p for p in active_players()
                if not p["all_in"]
            ),
            None
        )

    set_turn(first)


def advance_stage():
    if game["stage"] == "preflop":
        burn_card()

        for _ in range(3):
            game["community_cards"].append(deal_one())

        game["stage"] = "flop"

    elif game["stage"] == "flop":
        burn_card()
        game["community_cards"].append(deal_one())
        game["stage"] = "turn"

    elif game["stage"] == "turn":
        burn_card()
        game["community_cards"].append(deal_one())
        game["stage"] = "river"

    elif game["stage"] == "river":
        finish_hand()
        return

    for p in seated_players():
        if not p["folded"]:
            p["bet"] = 0

    game["current_bet"] = 0

    dealer = player_by_seat(game["dealer_seat"])

    first = None

    if dealer:
        first = next_actionable_from_seat(dealer["seat"])

    if first is None:
        first = next(
            (
                p for p in active_players()
                if not p["all_in"]
            ),
            None
        )

    game["message"] = f"{game['stage'].title()} dealt"

    set_turn(first)


def finish_hand():
    remaining = [
        p for p in seated_players()
        if not p["folded"]
    ]

    if not remaining:
        game["started"] = False
        game["stage"] = "waiting"
        game["message"] = "No winner"
        set_turn(None)
        return

    if len(remaining) == 1:
        winner = remaining[0]

    else:
        scored = []

        for p in remaining:
            score = evaluate_hand(
                p["cards"] + game["community_cards"]
            )

            scored.append((score, p))

        best_score = max(score for score, _ in scored)

        winners = [
            p for score, p in scored
            if score == best_score
        ]

        winner = winners[0]

    pot = calculate_pot()

    if len(remaining) > 1:
        winners = []

        scores = []

        for p in remaining:
            score = evaluate_hand(
                p["cards"] + game["community_cards"]
            )
            scores.append((score, p))

        best_score = max(x[0] for x in scores)

        winners = [
            p for score, p in scores
            if score == best_score
        ]

        share = pot // len(winners)

        for p in winners:
            p["chips"] += share

        winner_names = ", ".join(p["name"] for p in winners)

        game["winner"] = {
            "user_ids": [p["user_id"] for p in winners],
            "names": [p["name"] for p in winners],
            "amount": share,
            "hand": hand_name(best_score),
        }

        game["message"] = (
            f"{winner_names} won {share} chips "
            f"with {hand_name(best_score)}"
        )

    else:
        winner["chips"] += pot

        game["winner"] = {
            "user_ids": [winner["user_id"]],
            "names": [winner["name"]],
            "amount": pot,
            "hand": "Everyone else folded",
        }

        game["message"] = (
            f"{winner['name']} won {pot} chips"
        )

    game["started"] = False
    game["stage"] = "showdown"
    game["current_player"] = None
    game["turn_started_at"] = None
    game["turn_deadline"] = None


# =========================================================
# ACTION ENGINE
# =========================================================

async def process_action(
    user_id: str,
    action: str,
    amount: int = 0
):
    user_id = str(user_id)

    p = find_player(user_id)

    if not p:
        raise HTTPException(
            status_code=404,
            detail="Player not found"
        )

    if not game["started"]:
        raise HTTPException(
            status_code=400,
            detail="Game is not running"
        )

    if game["current_player"] != user_id:
        raise HTTPException(
            status_code=400,
            detail="Not your turn"
        )

    if p["folded"] or p["all_in"]:
        raise HTTPException(
            status_code=400,
            detail="Player cannot act"
        )

    action = normalize_action(action)

    call_amount = max(
        0,
        game["current_bet"] - p["bet"]
    )

    if action == "fold":

        p["folded"] = True

        game["last_action"] = f"{p['name']} folded"

    elif action == "check":

        if call_amount != 0:
            raise HTTPException(
                status_code=400,
                detail="Cannot check"
            )

        game["last_action"] = f"{p['name']} checked"

    elif action == "call":

        pay = min(call_amount, p["chips"])

        p["chips"] -= pay
        p["bet"] += pay
        p["total_bet"] += pay

        if p["chips"] == 0:
            p["all_in"] = True

        game["last_action"] = f"{p['name']} called"

    elif action == "raise":

        requested = int(amount)

        if requested <= game["current_bet"]:
            raise HTTPException(
                status_code=400,
                detail="Raise must be higher than current bet"
            )

        target = requested

        additional = target - p["bet"]

        if additional <= 0:
            raise HTTPException(
                status_code=400,
                detail="Invalid raise"
            )

        additional = min(additional, p["chips"])

        p["chips"] -= additional
        p["bet"] += additional
        p["total_bet"] += additional

        if p["bet"] > game["current_bet"]:
            game["current_bet"] = p["bet"]

        if p["chips"] == 0:
            p["all_in"] = True

        game["last_action"] = (
            f"{p['name']} raised to {p['bet']}"
        )

    elif action == "allin":

        if p["chips"] <= 0:
            raise HTTPException(
                status_code=400,
                detail="No chips"
            )

        allin_amount = p["chips"]

        p["chips"] = 0
        p["bet"] += allin_amount
        p["total_bet"] += allin_amount
        p["all_in"] = True

        if p["bet"] > game["current_bet"]:
            game["current_bet"] = p["bet"]

        game["last_action"] = f"{p['name']} went all-in"

    else:
        raise HTTPException(
            status_code=400,
            detail="Unknown action"
        )

    game["pot"] = calculate_pot()

    remaining = [
        x for x in active_players()
    ]

    if len(remaining) == 1:
        finish_hand()
        await broadcast()
        return public_game_state(user_id)

    if len(actionable_players()) == 0:
        while game["stage"] != "showdown":
            if game["stage"] == "river":
                finish_hand()
                break

            advance_stage()

        await broadcast()
        return public_game_state(user_id)

    if everyone_acted_or_allin():
        if game["stage"] == "river":
            finish_hand()
        else:
            advance_stage()

        await broadcast()
        return public_game_state(user_id)

    nxt = next_actionable_from_seat(p["seat"])

    set_turn(nxt)

    game["message"] = game["last_action"]

    await broadcast()

    if nxt and nxt["is_bot"]:
        asyncio.create_task(bot_turn())

    return public_game_state(user_id)


# =========================================================
# ROUTES
# =========================================================

@app.get("/")
def root():
    return {
        "status": "online",
        "message": "Telegram Poker Backend is running",
        "version": "1.0",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "players": len(players),
        "started": game["started"],
    }


@app.get("/players")
def get_players():
    return {
        "success": True,
        "players": [
            visible_player(p)
            for p in seated_players()
        ],
    }


@app.get("/game")
def get_game(user_id: Optional[str] = None):
    return public_game_state(user_id)


@app.get("/my-cards")
def my_cards(user_id: str):
    p = find_player(user_id)

    if not p:
        raise HTTPException(
            status_code=404,
            detail="Player not found"
        )

    return {
        "success": True,
        "user_id": user_id,
        "cards": [
            card_text(c)
            for c in p["cards"]
        ],
    }


@app.post("/join")
async def join(req: JoinRequest):
    uid = str(req.user_id)

    if uid in players:
        players[uid]["name"] = req.name or players[uid]["name"]

        await broadcast()

        return {
            "success": True,
            "message": "Already seated",
            "player": visible_player(players[uid]),
        }

    used_seats = {
        p["seat"]
        for p in seated_players()
    }

    free_seat = None

    for seat in range(MAX_SEATS):
        if seat not in used_seats:
            free_seat = seat
            break

    if free_seat is None:
        raise HTTPException(
            status_code=400,
            detail="Table is full"
        )

    players[uid] = {
        "user_id": uid,
        "name": req.name or "Player",
        "chips": STARTING_CHIPS,
        "seat": free_seat,
        "cards": [],
        "folded": False,
        "all_in": False,
        "bet": 0,
        "total_bet": 0,
        "is_bot": False,
    }

    game["message"] = f"{req.name} joined the table"

    await broadcast()

    return {
        "success": True,
        "message": "Joined table",
        "player": visible_player(players[uid]),
    }


@app.post("/sit")
async def sit(req: SitRequest):
    uid = str(req.user_id)

    if uid in players:
        return {
            "success": True,
            "player": visible_player(players[uid]),
        }

    used_seats = {
        p["seat"]
        for p in seated_players()
    }

    selected_seat = req.seat

    if selected_seat is not None:
        if selected_seat in used_seats:
            raise HTTPException(
                status_code=400,
                detail="Seat is occupied"
            )

        if selected_seat < 0 or selected_seat >= MAX_SEATS:
            raise HTTPException(
                status_code=400,
                detail="Invalid seat"
            )
    else:
        selected_seat = next(
            (
                s for s in range(MAX_SEATS)
                if s not in used_seats
            ),
            None
        )

    if selected_seat is None:
        raise HTTPException(
            status_code=400,
            detail="Table is full"
        )

    players[uid] = {
        "user_id": uid,
        "name": req.name or "Player",
        "chips": STARTING_CHIPS,
        "seat": selected_seat,
        "cards": [],
        "folded": False,
        "all_in": False,
        "bet": 0,
        "total_bet": 0,
        "is_bot": False,
    }

    await broadcast()

    return {
        "success": True,
        "player": visible_player(players[uid]),
    }


@app.post("/start")
async def start_game():
    if len(seated_players()) < 2:
        raise HTTPException(
            status_code=400,
            detail="At least 2 players are required"
        )

    if game["started"]:
        return {
            "success": True,
            "message": "Game already started",
            **public_game_state(),
        }

    start_new_hand()

    await broadcast()

    current = find_player(game["current_player"])

    if current and current["is_bot"]:
        asyncio.create_task(bot_turn())

    return {
        "success": True,
        **public_game_state(),
    }


@app.post("/action")
async def action(req: ActionRequest):
    return await process_action(
        req.user_id,
        req.action,
        req.amount,
    )


@app.post("/chat")
async def chat(req: ChatRequest):
    p = find_player(req.user_id)

    if not p:
        raise HTTPException(
            status_code=404,
            detail="Player not found"
        )

    message = req.message.strip()

    if not message:
        raise HTTPException(
            status_code=400,
            detail="Empty message"
        )

    if len(message) > 200:
        message = message[:200]

    game["chat"].append({
        "user_id": req.user_id,
        "name": p["name"],
        "message": message,
        "time": int(time.time()),
    })

    await broadcast()

    return {
        "success": True
    }


@app.post("/reset")
async def reset():
    game["started"] = False
    game["hand_number"] = 0
    game["stage"] = "waiting"
    game["deck"] = []
    game["community_cards"] = []
    game["pot"] = 0
    game["current_bet"] = 0
    game["current_player"] = None
    game["winner"] = None
    game["message"] = "Game reset"
    game["last_action"] = ""
    game["turn_started_at"] = None
    game["turn_deadline"] = None

    for p in players.values():
        p["chips"] = STARTING_CHIPS
        reset_player_for_hand(p)

    await broadcast()

    return {
        "success": True,
        "message": "Game reset",
    }


@app.post("/add-bots")
async def add_bots():
    bot_number = 1

    for seat in range(MAX_SEATS):
        if player_by_seat(seat):
            continue

        uid = f"bot_{seat + 1}"

        while uid in players:
            bot_number += 1
            uid = f"bot_{bot_number}"

        players[uid] = {
            "user_id": uid,
            "name": f"Bot {seat + 1}",
            "chips": STARTING_CHIPS,
            "seat": seat,
            "cards": [],
            "folded": False,
            "all_in": False,
            "bet": 0,
            "total_bet": 0,
            "is_bot": True,
        }

    await broadcast()

    return {
        "success": True,
        "players": [
            visible_player(p)
            for p in seated_players()
        ],
    }


# =========================================================
# WEBSOCKET
# =========================================================

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    user_id: str
):
    await websocket.accept()

    uid = str(user_id)

    websockets[uid] = websocket

    try:
        await websocket.send_json(
            public_game_state(uid)
        )

        while True:
            data = await websocket.receive_json()

            if data.get("type") == "ping":
                await websocket.send_json({
                    "type": "pong"
                })

            elif data.get("type") == "action":
                try:
                    await process_action(
                        uid,
                        data.get("action", ""),
                        int(data.get("amount", 0)),
                    )
                except Exception as e:
                    await websocket.send_json({
                        "success": False,
                        "error": str(e),
                    })

            elif data.get("type") == "chat":
                message = str(
                    data.get("message", "")
                ).strip()

                p = find_player(uid)

                if p and message:
                    game["chat"].append({
                        "user_id": uid,
                        "name": p["name"],
                        "message": message[:200],
                        "time": int(time.time()),
                    })

                    await broadcast()

    except WebSocketDisconnect:
        if websockets.get(uid) is websocket:
            websockets.pop(uid, None)

    except Exception:
        if websockets.get(uid) is websocket:
            websockets.pop(uid, None)


# =========================================================
# TURN TIMER
# =========================================================

async def timer_loop():
    while True:
        try:
            await asyncio.sleep(1)

            if not game["started"]:
                continue

            uid = game["current_player"]

            if not uid:
                continue

            deadline = game["turn_deadline"]

            if not deadline:
                continue

            if time.time() < deadline:
                continue

            p = find_player(uid)

            if not p:
                continue

            if game["current_player"] != uid:
                continue

            if p["folded"] or p["all_in"]:
                continue

            call_amount = max(
                0,
                game["current_bet"] - p["bet"]
            )

            if call_amount == 0:
                auto_action = "check"
            else:
                auto_action = "fold"

            try:
                await process_action(
                    uid,
                    auto_action,
                    0
                )
            except Exception:
                pass

        except Exception:
            await asyncio.sleep(1)


@app.on_event("startup")
async def startup():
    asyncio.create_task(timer_loop())
