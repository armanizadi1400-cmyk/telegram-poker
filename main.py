from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import random
import asyncio
import time

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
SUITS = "SHDC"

SUIT_SYMBOLS = {
    "S": "♠",
    "H": "♥",
    "D": "♦",
    "C": "♣",
}

BOT_NAMES = [
    "Alex",
    "Daniel",
    "Michael",
    "David",
    "Chris",
]

# =========================================================
# DATA
# =========================================================

players = {}

game = {
    "started": False,
    "stage": "waiting",
    "deck": [],
    "community_cards": [],
    "pot": 0,
    "current_player": None,
    "current_bet": 0,
    "dealer_index": -1,
    "acted_players": set(),
    "winner": None,
    "message": "",
    "hand_number": 0,
    "turn_started_at": None,
}

connections = {}


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
    seat: int


class ChatRequest(BaseModel):
    user_id: str
    message: str


# =========================================================
# CARD HELPERS
# =========================================================

def make_deck():
    return [r + s for r in RANKS for s in SUITS]


def card_text(card):
    if not card or len(card) < 2:
        return card

    return card[0] + SUIT_SYMBOLS.get(card[1], card[1])


def cards_text(cards):
    return [card_text(c) for c in cards]


def rank_value(card):
    return RANKS.index(card[0]) + 2


# =========================================================
# PLAYER HELPERS
# =========================================================

def seated_players():
    return sorted(
        [p for p in players.values() if p.get("seat") is not None],
        key=lambda x: x["seat"]
    )


def real_players():
    return [p for p in seated_players() if not p.get("is_bot", False)]


def bot_players():
    return [p for p in seated_players() if p.get("is_bot", False)]


def get_player(user_id):
    return players.get(str(user_id))


def available_seat():
    used = {p["seat"] for p in seated_players()}

    for seat in range(MAX_SEATS):
        if seat not in used:
            return seat

    return None


def next_seat(seat):
    occupied = {p["seat"] for p in seated_players()}

    if not occupied:
        return None

    for i in range(1, MAX_SEATS + 1):
        candidate = (seat + i) % MAX_SEATS

        if candidate in occupied:
            return candidate

    return None


def player_can_act(player):
    if not player:
        return False

    if player.get("folded"):
        return False

    if player.get("all_in"):
        return False

    if player.get("chips", 0) <= 0:
        return False

    return True


# =========================================================
# POT
# =========================================================

def calculate_pot():
    return sum(p.get("total_bet", 0) for p in seated_players())


def update_pot():
    game["pot"] = calculate_pot()


# =========================================================
# WEBSOCKET
# =========================================================

async def broadcast():
    dead = []

    for user_id, ws in connections.items():
        try:
            await ws.send_json(public_game_state(user_id))
        except Exception:
            dead.append(user_id)

    for user_id in dead:
        connections.pop(user_id, None)


# =========================================================
# GAME STATE
# =========================================================

def public_game_state(user_id=None):
    public_players = []

    for p in seated_players():
        public_players.append({
            "user_id": p["user_id"],
            "name": p["name"],
            "chips": p["chips"],
            "seat": p["seat"],
            "bet": p["bet"],
            "total_bet": p["total_bet"],
            "folded": p["folded"],
            "all_in": p["all_in"],
            "is_bot": p["is_bot"],
            "is_turn": p["user_id"] == game["current_player"],
            "cards_count": len(p["cards"]),
        })

    my_cards = []

    if user_id and user_id in players:
        my_cards = cards_text(players[user_id]["cards"])

    return {
        "success": True,
        "started": game["started"],
        "stage": game["stage"],
        "community_cards": cards_text(game["community_cards"]),
        "pot": game["pot"],
        "current_player": game["current_player"],
        "current_bet": game["current_bet"],
        "winner": game["winner"],
        "message": game["message"],
        "hand_number": game["hand_number"],
        "turn_started_at": game["turn_started_at"],
        "players": public_players,
        "my_cards": my_cards,
    }


# =========================================================
# DEAL
# =========================================================

def deal_card():
    if not game["deck"]:
        raise HTTPException(status_code=400, detail="No cards left")

    return game["deck"].pop()


def deal_hole_cards():
    for p in seated_players():
        p["cards"] = []

    for _ in range(2):
        for p in seated_players():
            p["cards"].append(deal_card())


# =========================================================
# BLINDS
# =========================================================

def post_blind(player, amount):
    actual = min(amount, player["chips"])

    player["chips"] -= actual
    player["bet"] += actual
    player["total_bet"] += actual

    if player["chips"] == 0:
        player["all_in"] = True

    return actual


# =========================================================
# TURN HELPERS
# =========================================================

def actionable_players():
    return [
        p for p in seated_players()
        if player_can_act(p)
    ]


def next_actionable_from_seat(seat):
    occupied = seated_players()

    if not occupied:
        return None

    for i in range(1, MAX_SEATS + 1):
        candidate_seat = (seat + i) % MAX_SEATS

        for p in occupied:
            if p["seat"] == candidate_seat and player_can_act(p):
                return p

    return None


def all_active_have_equal_bet():
    active = [
        p for p in seated_players()
        if not p["folded"] and not p["all_in"]
    ]

    if not active:
        return True

    return all(p["bet"] == game["current_bet"] for p in active)


def only_one_active_player():
    active = [
        p for p in seated_players()
        if not p["folded"]
    ]

    return len(active) == 1


# =========================================================
# HAND EVALUATION
# =========================================================

def evaluate_five(cards):
    values = sorted(
        [rank_value(c) for c in cards],
        reverse=True
    )

    suits = [c[1] for c in cards]

    counts = {}

    for v in values:
        counts[v] = counts.get(v, 0) + 1

    unique_values = sorted(set(values), reverse=True)

    if 14 in unique_values:
        unique_values.append(1)

    straight_high = None

    for i in range(len(unique_values) - 4):
        seq = unique_values[i:i + 5]

        if seq[0] - seq[4] == 4:
            straight_high = seq[0]
            break

    flush = len(set(suits)) == 1

    if flush and straight_high:
        return (8, straight_high)

    groups = sorted(
        [(count, value) for value, count in counts.items()],
        reverse=True
    )

    four = [v for v, c in counts.items() if c == 4]

    if four:
        kicker = max(v for v in values if v != four[0])
        return (7, four[0], kicker)

    trips = sorted(
        [v for v, c in counts.items() if c == 3],
        reverse=True
    )

    pairs = sorted(
        [v for v, c in counts.items() if c == 2],
        reverse=True
    )

    if trips and (len(trips) >= 2 or pairs):
        trip = trips[0]

        if len(trips) >= 2:
            pair = trips[1]
        else:
            pair = pairs[0]

        return (6, trip, pair)

    if flush:
        return (5, *values)

    if straight_high:
        return (4, straight_high)

    if trips:
        kickers = sorted(
            [v for v in values if v != trips[0]],
            reverse=True
        )[:2]

        return (3, trips[0], *kickers)

    if len(pairs) >= 2:
        pair1 = pairs[0]
        pair2 = pairs[1]

        kicker = max(
            v for v in values
            if v != pair1 and v != pair2
        )

        return (2, pair1, pair2, kicker)

    if len(pairs) == 1:
        pair = pairs[0]

        kickers = sorted(
            [v for v in values if v != pair],
            reverse=True
        )[:3]

        return (1, pair, *kickers)

    return (0, *values)


def best_hand(cards):
    if len(cards) < 5:
        values = sorted(
            [rank_value(c) for c in cards],
            reverse=True
        )

        return (0, *values)

    best = None

    from itertools import combinations

    for combo in combinations(cards, 5):
        score = evaluate_five(combo)

        if best is None or score > best:
            best = score

    return best


# =========================================================
# WINNER
# =========================================================

def finish_hand():
    active = [
        p for p in seated_players()
        if not p["folded"]
    ]

    if not active:
        game["winner"] = None
        game["started"] = False
        game["stage"] = "waiting"
        return

    if len(active) == 1:
        winner = active[0]

        prize = calculate_pot()
        winner["chips"] += prize

        game["winner"] = winner["user_id"]
        game["message"] = f"{winner['name']} wins {prize} chips"
    else:
        scored = []

        for p in active:
            seven_cards = p["cards"] + game["community_cards"]
            score = best_hand(seven_cards)

            scored.append((score, p))

        best_score = max(score for score, _ in scored)

        winners = [
            p for score, p in scored
            if score == best_score
        ]

        prize = calculate_pot()
        share = prize // len(winners)

        for winner in winners:
            winner["chips"] += share

        names = ", ".join(w["name"] for w in winners)

        game["winner"] = winners[0]["user_id"]
        game["message"] = f"{names} wins {share} chips"

    update_pot()

    game["started"] = False
    game["current_player"] = None
    game["turn_started_at"] = None


# =========================================================
# NEXT STAGE
# =========================================================

def advance_stage():
    game["acted_players"] = set()

    for p in seated_players():
        p["bet"] = 0

    game["current_bet"] = 0

    if game["stage"] == "preflop":
        game["community_cards"] = [
            deal_card(),
            deal_card(),
            deal_card(),
        ]

        game["stage"] = "flop"

    elif game["stage"] == "flop":
        game["community_cards"].append(deal_card())
        game["stage"] = "turn"

    elif game["stage"] == "turn":
        game["community_cards"].append(deal_card())
        game["stage"] = "river"

    elif game["stage"] == "river":
        finish_hand()
        return

    dealer = game["dealer_index"]

    next_player = next_actionable_from_seat(dealer)

    if next_player:
        game["current_player"] = next_player["user_id"]
        game["turn_started_at"] = time.time()
    else:
        finish_hand()


# =========================================================
# ACTION
# =========================================================

def perform_action(user_id, action, amount=0):
    player = get_player(user_id)

    if not player:
        raise HTTPException(
            status_code=404,
            detail="Player not found"
        )

    if not game["started"]:
        raise HTTPException(
            status_code=400,
            detail="Game is not started"
        )

    if game["current_player"] != user_id:
        raise HTTPException(
            status_code=400,
            detail="It is not your turn"
        )

    if not player_can_act(player):
        raise HTTPException(
            status_code=400,
            detail="Player cannot act"
        )

    action = action.lower().strip()

    if action == "fold":

        player["folded"] = True
        game["acted_players"].add(user_id)

        if only_one_active_player():
            finish_hand()
            return

    elif action == "check":

        if player["bet"] != game["current_bet"]:
            raise HTTPException(
                status_code=400,
                detail="Cannot check. You must call or raise."
            )

        game["acted_players"].add(user_id)

    elif action == "call":

        needed = game["current_bet"] - player["bet"]

        if needed <= 0:
            game["acted_players"].add(user_id)
        else:
            actual = min(needed, player["chips"])

            player["chips"] -= actual
            player["bet"] += actual
            player["total_bet"] += actual

            if player["chips"] == 0:
                player["all_in"] = True

            game["acted_players"].add(user_id)

    elif action == "raise":

        requested = int(amount)

        if requested <= game["current_bet"]:
            raise HTTPException(
                status_code=400,
                detail="Raise must be greater than current bet"
            )

        additional = requested - player["bet"]

        if additional > player["chips"]:
            additional = player["chips"]

        player["chips"] -= additional
        player["bet"] += additional
        player["total_bet"] += additional

        game["current_bet"] = player["bet"]

        if player["chips"] == 0:
            player["all_in"] = True

        game["acted_players"] = {user_id}

    elif action in ("allin", "all-in"):

        additional = player["chips"]

        if additional <= 0:
            player["all_in"] = True
        else:
            player["chips"] = 0
            player["bet"] += additional
            player["total_bet"] += additional

            if player["bet"] > game["current_bet"]:
                game["current_bet"] = player["bet"]

            player["all_in"] = True

        game["acted_players"].add(user_id)

    else:
        raise HTTPException(
            status_code=400,
            detail="Invalid action"
        )

    update_pot()

    # Check whether hand should advance
    if only_one_active_player():
        finish_hand()
        return

    if all_active_have_equal_bet():
        advance_stage()
        return

    next_player = next_actionable_from_seat(player["seat"])

    if next_player:
        game["current_player"] = next_player["user_id"]
        game["turn_started_at"] = time.time()
    else:
        advance_stage()


# =========================================================
# BOTS
# =========================================================

def create_bot():
    seat = available_seat()

    if seat is None:
        return None

    existing_names = {
        p["name"]
        for p in seated_players()
    }

    available_names = [
        name
        for name in BOT_NAMES
        if name not in existing_names
    ]

    name = (
        random.choice(available_names)
        if available_names
        else f"Bot {seat + 1}"
    )

    bot_id = f"bot_{seat}_{random.randint(1000, 9999)}"

    players[bot_id] = {
        "user_id": bot_id,
        "name": name,
        "chips": STARTING_CHIPS,
        "seat": seat,
        "cards": [],
        "folded": False,
        "all_in": False,
        "bet": 0,
        "total_bet": 0,
        "is_bot": True,
    }

    return players[bot_id]


def add_bots_until(count):
    while len(seated_players()) < count:
        if create_bot() is None:
            break


def bot_decision(bot):
    if not game["started"]:
        return

    if game["current_player"] != bot["user_id"]:
        return

    if not player_can_act(bot):
        return

    to_call = game["current_bet"] - bot["bet"]

    strength = random.random()

    # Very weak hand
    if strength < 0.15:
        if to_call == 0:
            action = "check"
            amount = 0
        else:
            action = "fold"
            amount = 0

    # Normal call
    elif strength < 0.75:
        if to_call == 0:
            action = "check"
            amount = 0
        else:
            action = "call"
            amount = 0

    # Raise
    elif strength < 0.93:
        raise_to = max(
            game["current_bet"] + BIG_BLIND,
            bot["bet"] + BIG_BLIND
        )

        action = "raise"
        amount = min(
            raise_to,
            bot["bet"] + bot["chips"]
        )

    else:
        action = "allin"
        amount = 0

    perform_action(
        bot["user_id"],
        action,
        amount
    )


async def run_bot_turn():
    await asyncio.sleep(1)

    if not game["started"]:
        return

    current_id = game["current_player"]

    if not current_id:
        return

    player = players.get(current_id)

    if not player:
        return

    if not player.get("is_bot"):
        return

    try:
        bot_decision(player)
    except Exception:
        # Safety fallback
        try:
            if game["current_player"] == player["user_id"]:
                if game["current_bet"] == player["bet"]:
                    perform_action(
                        player["user_id"],
                        "check"
                    )
                else:
                    perform_action(
                        player["user_id"],
                        "call"
                    )
        except Exception:
            pass

    await broadcast()


# =========================================================
# START HAND
# =========================================================

def start_new_hand():

    seated = seated_players()

    if len(seated) < 2:
        raise HTTPException(
            status_code=400,
            detail="At least 2 players are required"
        )

    game["started"] = True
    game["stage"] = "preflop"
    game["deck"] = make_deck()
    random.shuffle(game["deck"])

    game["community_cards"] = []
    game["pot"] = 0
    game["current_bet"] = 0
    game["acted_players"] = set()
    game["winner"] = None
    game["message"] = ""
    game["hand_number"] += 1

    for p in seated:
        p["folded"] = False
        p["all_in"] = False
        p["bet"] = 0
        p["total_bet"] = 0
        p["cards"] = []

    # Rotate dealer
    occupied_seats = [p["seat"] for p in seated]

    if game["dealer_index"] not in occupied_seats:
        game["dealer_index"] = occupied_seats[0]
    else:
        nxt = next_seat(game["dealer_index"])

        if nxt is not None:
            game["dealer_index"] = nxt

    dealer = game["dealer_index"]

    # Heads-up
    if len(seated) == 2:
        sb_seat = dealer

        bb_seat = next_seat(dealer)

        sb_player = next(
            p for p in seated
            if p["seat"] == sb_seat
        )

        bb_player = next(
            p for p in seated
            if p["seat"] == bb_seat
        )

    else:
        sb_seat = next_seat(dealer)
        bb_seat = next_seat(sb_seat)

        sb_player = next(
            p for p in seated
            if p["seat"] == sb_seat
        )

        bb_player = next(
            p for p in seated
            if p["seat"] == bb_seat
        )

    post_blind(sb_player, SMALL_BLIND)
    post_blind(bb_player, BIG_BLIND)

    game["current_bet"] = BIG_BLIND

    deal_hole_cards()

    # Preflop action starts left of big blind
    if len(seated) == 2:
        first = sb_player
    else:
        first = next_actionable_from_seat(bb_player["seat"])

    if first:
        game["current_player"] = first["user_id"]
        game["turn_started_at"] = time.time()
    else:
        advance_stage()

    update_pot()


# =========================================================
# ROUTES
# =========================================================

@app.get("/")
def root():
    return {
        "status": "online",
        "message": "Poker backend is running",
        "version": "2.0",
    }


@app.get("/health")
def health():
    return {
        "status": "ok"
    }


@app.get("/players")
def get_players():
    return {
        "success": True,
        "players": [
            {
                "user_id": p["user_id"],
                "name": p["name"],
                "chips": p["chips"],
                "seat": p["seat"],
                "bet": p["bet"],
                "total_bet": p["total_bet"],
                "folded": p["folded"],
                "all_in": p["all_in"],
                "is_bot": p["is_bot"],
                "is_turn": p["user_id"] == game["current_player"],
                "cards_count": len(p["cards"]),
            }
            for p in seated_players()
        ],
    }


@app.get("/game")
def get_game(user_id: Optional[str] = None):
    return public_game_state(user_id)


@app.get("/my-cards")
def my_cards(user_id: str):
    player = get_player(user_id)

    if not player:
        raise HTTPException(
            status_code=404,
            detail="Player not found"
        )

    return {
        "success": True,
        "user_id": user_id,
        "cards": cards_text(player["cards"]),
    }


@app.post("/join")
async def join(request: JoinRequest):

    user_id = str(request.user_id)

    existing = players.get(user_id)

    if existing:
        return {
            "success": True,
            "message": "Already joined",
            "player": existing,
        }

    seat = available_seat()

    # If table is full before game starts, replace a bot
    if seat is None and not game["started"]:
        bots = bot_players()

        if bots:
            old_bot = bots[-1]
            seat = old_bot["seat"]
            players.pop(old_bot["user_id"], None)

    if seat is None:
        raise HTTPException(
            status_code=400,
            detail="Table is full"
        )

    players[user_id] = {
        "user_id": user_id,
        "name": request.name or "Player",
        "chips": STARTING_CHIPS,
        "seat": seat,
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
        "message": "Player joined",
        "player": players[user_id],
    }


@app.post("/sit")
async def sit(request: SitRequest):

    player = get_player(request.user_id)

    if not player:
        raise HTTPException(
            status_code=404,
            detail="Player not found"
        )

    if game["started"]:
        raise HTTPException(
            status_code=400,
            detail="Cannot change seat during game"
        )

    if request.seat < 0 or request.seat >= MAX_SEATS:
        raise HTTPException(
            status_code=400,
            detail="Invalid seat"
        )

    for p in seated_players():
        if p["seat"] == request.seat and p["user_id"] != request.user_id:
            raise HTTPException(
                status_code=400,
                detail="Seat occupied"
            )

    player["seat"] = request.seat

    await broadcast()

    return {
        "success": True,
        "player": player,
    }


@app.post("/add-bots")
async def add_bots():

    add_bots_until(MAX_SEATS)

    await broadcast()

    return {
        "success": True,
        "message": "Bots added",
        "players": [
            {
                "user_id": p["user_id"],
                "name": p["name"],
                "seat": p["seat"],
                "is_bot": p["is_bot"],
            }
            for p in seated_players()
        ],
    }


@app.post("/start")
async def start_game():

    if game["started"]:
        raise HTTPException(
            status_code=400,
            detail="Game already started"
        )

    # IMPORTANT:
    # One real player is enough.
    # Automatically add bots until there are 6 players.
    if len(seated_players()) == 1:
        add_bots_until(MAX_SEATS)

    elif len(seated_players()) < 2:
        add_bots_until(2)

    start_new_hand()

    await broadcast()

    # If first turn is bot
    if game["current_player"]:
        current = players.get(game["current_player"])

        if current and current.get("is_bot"):
            asyncio.create_task(run_bot_turn())

    return public_game_state()


@app.post("/action")
async def action(request: ActionRequest):

    perform_action(
        request.user_id,
        request.action,
        request.amount
    )

    await broadcast()

    if game["started"] and game["current_player"]:
        current = players.get(game["current_player"])

        if current and current.get("is_bot"):
            asyncio.create_task(run_bot_turn())

    return public_game_state(request.user_id)


@app.post("/chat")
async def chat(request: ChatRequest):

    player = get_player(request.user_id)

    if not player:
        raise HTTPException(
            status_code=404,
            detail="Player not found"
        )

    message = request.message.strip()

    if not message:
        raise HTTPException(
            status_code=400,
            detail="Message is empty"
        )

    return {
        "success": True,
        "user_id": request.user_id,
        "name": player["name"],
        "message": message,
    }


@app.post("/reset")
async def reset():

    players.clear()

    game["started"] = False
    game["stage"] = "waiting"
    game["deck"] = []
    game["community_cards"] = []
    game["pot"] = 0
    game["current_player"] = None
    game["current_bet"] = 0
    game["dealer_index"] = -1
    game["acted_players"] = set()
    game["winner"] = None
    game["message"] = ""
    game["hand_number"] = 0
    game["turn_started_at"] = None

    await broadcast()

    return {
        "success": True,
        "message": "Game reset"
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

    connections[user_id] = websocket

    try:
        await websocket.send_json(
            public_game_state(user_id)
        )

        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        connections.pop(user_id, None)

    except Exception:
        connections.pop(user_id, None)


# =========================================================
# TURN TIMER
# =========================================================

async def timer_loop():

    while True:

        await asyncio.sleep(1)

        if not game["started"]:
            continue

        current_id = game["current_player"]

        if not current_id:
            continue

        player = players.get(current_id)

        if not player:
            continue

        # Bot
        if player.get("is_bot"):
            continue

        started_at = game.get("turn_started_at")

        if not started_at:
            game["turn_started_at"] = time.time()
            continue

        elapsed = time.time() - started_at

        if elapsed >= TURN_SECONDS:

            try:
                # Auto check if possible
                if player["bet"] == game["current_bet"]:
                    perform_action(
                        current_id,
                        "check"
                    )
                else:
                    perform_action(
                        current_id,
                        "fold"
                    )

                await broadcast()

                if game["started"] and game["current_player"]:

                    current = players.get(
                        game["current_player"]
                    )

                    if current and current.get("is_bot"):
                        asyncio.create_task(
                            run_bot_turn()
                        )

            except Exception:
                pass


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(timer_loop())
