from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import random
import asyncio
import itertools
from collections import Counter

app = FastAPI(title="Texas Holdem Poker")

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

TABLE_COUNT = 6
MAX_SEATS = 6
BOT_COUNT = 3

STARTING_CHIPS = 1000
SMALL_BLIND = 10
BIG_BLIND = 20

BOT_NAMES = [
    "Daniel", "Alex", "Michael",
    "Ryan", "David", "Kevin",
    "James", "Robert", "Thomas",
    "Jack", "William", "Chris",
    "Adam", "Sam", "John",
    "Mark", "Leo", "Nick"
]

RANKS = "23456789TJQKA"
SUITS = ["♠", "♥", "♦", "♣"]

# =========================================================
# CARD ENGINE
# =========================================================

def new_deck():
    cards = [r + s for r in RANKS for s in SUITS]
    random.shuffle(cards)
    return cards


def card_value(card):
    return RANKS.index(card[0]) + 2


def evaluate_five(cards):
    values = sorted([card_value(c) for c in cards], reverse=True)
    suits = [c[1] for c in cards]

    counts = Counter(values)
    groups = sorted(
        ((count, value) for value, count in counts.items()),
        reverse=True
    )

    flush = len(set(suits)) == 1

    unique = sorted(set(values), reverse=True)

    if 14 in unique:
        unique.append(1)

    straight_high = None

    for i in range(len(unique) - 4):
        seq = unique[i:i + 5]
        if seq[0] - seq[4] == 4:
            straight_high = seq[0]
            break

    if flush and straight_high:
        return (8, straight_high)

    quads = [v for v, c in counts.items() if c == 4]
    if quads:
        q = max(quads)
        kicker = max(v for v in values if v != q)
        return (7, q, kicker)

    trips = sorted([v for v, c in counts.items() if c == 3], reverse=True)
    pairs = sorted([v for v, c in counts.items() if c == 2], reverse=True)

    if trips:
        if len(trips) >= 2:
            return (6, trips[0], trips[1])

        if pairs:
            return (6, trips[0], pairs[0])

    if flush:
        return (5, *values)

    if straight_high:
        return (4, straight_high)

    if trips:
        t = trips[0]
        kickers = sorted([v for v in values if v != t], reverse=True)[:2]
        return (3, t, *kickers)

    if len(pairs) >= 2:
        p1, p2 = pairs[:2]
        kicker = max(v for v in values if v != p1 and v != p2)
        return (2, p1, p2, kicker)

    if len(pairs) == 1:
        p = pairs[0]
        kickers = sorted([v for v in values if v != p], reverse=True)[:3]
        return (1, p, *kickers)

    return (0, *values)


def evaluate_seven(cards):
    best = None

    for combo in itertools.combinations(cards, 5):
        score = evaluate_five(combo)

        if best is None or score > best:
            best = score

    return best


def hand_name(score):
    names = [
        "High Card",
        "Pair",
        "Two Pair",
        "Three of a Kind",
        "Straight",
        "Flush",
        "Full House",
        "Four of a Kind",
        "Straight Flush"
    ]
    return names[score[0]]


# =========================================================
# DATA
# =========================================================

class JoinRequest(BaseModel):
    user_id: str
    name: str = "Player"
    table_id: int


class ActionRequest(BaseModel):
    user_id: str
    table_id: int
    action: str
    amount: int = 0


class Table:
    def __init__(self, table_id):
        self.id = table_id

        self.players = []
        self.waiting = []

        self.deck = []
        self.community = []
        self.pot = 0

        self.stage = "waiting"
        self.current_index = 0
        self.dealer_index = 0

        self.current_bet = 0
        self.min_raise = BIG_BLIND

        self.started = False
        self.hand_number = 0

        self.message = "Waiting for players"
        self.winner = None

        self.last_action = ""
        self.last_winner = []

        self.bot_task = None


tables = {
    i: Table(i)
    for i in range(1, TABLE_COUNT + 1)
}


# =========================================================
# PLAYER HELPERS
# =========================================================

def active_players(table):
    return [
        p for p in table.players
        if not p["folded"] and p["chips"] > 0
    ]


def seated_players(table):
    return table.players


def find_player(table, user_id):
    for p in table.players:
        if p["user_id"] == str(user_id):
            return p

    for p in table.waiting:
        if p["user_id"] == str(user_id):
            return p

    return None


def find_seated_player(table, user_id):
    for p in table.players:
        if p["user_id"] == str(user_id):
            return p
    return None


def real_players(table):
    return [
        p for p in table.players
        if not p["is_bot"]
    ]


def next_index(table, start, eligible_only=True):
    if not table.players:
        return None

    n = len(table.players)

    for step in range(1, n + 1):
        idx = (start + step) % n
        p = table.players[idx]

        if eligible_only:
            if not p["folded"] and p["chips"] > 0 and not p["all_in"]:
                return idx
        else:
            return idx

    return None


def reset_player_for_hand(p):
    p["cards"] = []
    p["folded"] = False
    p["all_in"] = False
    p["bet"] = 0
    p["total_bet"] = 0


def seat_waiting_players(table):
    if table.started:
        return

    while table.waiting and len(table.players) < MAX_SEATS:
        p = table.waiting.pop(0)
        table.players.append(p)


# =========================================================
# BLINDS / BETTING
# =========================================================

def post_blind(player, amount):
    amount = min(amount, player["chips"])

    player["chips"] -= amount
    player["bet"] += amount
    player["total_bet"] += amount

    if player["chips"] == 0:
        player["all_in"] = True

    return amount


def first_to_act_preflop(table):
    n = len(table.players)

    if n == 2:
        return (table.dealer_index + 1) % n

    return (table.dealer_index + 3) % n


def first_to_act_postflop(table):
    n = len(table.players)

    for step in range(1, n + 1):
        idx = (table.dealer_index + step) % n

        p = table.players[idx]

        if not p["folded"] and not p["all_in"] and p["chips"] >= 0:
            return idx

    return None


def all_equal_or_done(table):
    contenders = [
        p for p in table.players
        if not p["folded"] and not p["all_in"]
    ]

    if not contenders:
        return True

    for p in contenders:
        if p["bet"] != table.current_bet:
            return False

    return True


def betting_players(table):
    return [
        p for p in table.players
        if not p["folded"] and not p["all_in"]
    ]


# =========================================================
# HAND START
# =========================================================

def can_start(table):
    return len([p for p in table.players if p["chips"] > 0]) >= 2


def start_hand(table):
    seat_waiting_players(table)

    if not can_start(table):
        table.started = False
        table.stage = "waiting"
        table.message = "Waiting for players"
        return False

    table.hand_number += 1
    table.started = True
    table.stage = "preflop"

    table.deck = new_deck()
    table.community = []
    table.pot = 0
    table.current_bet = 0
    table.min_raise = BIG_BLIND
    table.winner = None
    table.last_winner = []
    table.last_action = ""

    for p in table.players:
        reset_player_for_hand(p)

    table.dealer_index %= len(table.players)

    # Deal two cards one at a time
    for _ in range(2):
        for p in table.players:
            if p["chips"] > 0:
                p["cards"].append(table.deck.pop())

    n = len(table.players)

    if n == 2:
        sb_index = table.dealer_index
        bb_index = (table.dealer_index + 1) % n
    else:
        sb_index = (table.dealer_index + 1) % n
        bb_index = (table.dealer_index + 2) % n

    sb = table.players[sb_index]
    bb = table.players[bb_index]

    post_blind(sb, SMALL_BLIND)
    post_blind(bb, BIG_BLIND)

    table.current_bet = max(
        p["bet"] for p in table.players
    )

    table.pot = sum(p["bet"] for p in table.players)

    table.current_index = first_to_act_preflop(table)

    table.message = "Preflop"

    return True


# =========================================================
# ROUND MANAGEMENT
# =========================================================

def advance_stage(table):
    # reset street bets
    for p in table.players:
        p["bet"] = 0

    table.current_bet = 0
    table.min_raise = BIG_BLIND

    if table.stage == "preflop":
        # FLOP = EXACTLY 3 CARDS
        if len(table.deck) < 3:
            finish_hand(table)
            return

        table.community.extend([
            table.deck.pop(),
            table.deck.pop(),
            table.deck.pop()
        ])

        table.stage = "flop"

    elif table.stage == "flop":
        # TURN = 1
        if len(table.deck) < 1:
            finish_hand(table)
            return

        table.community.append(table.deck.pop())
        table.stage = "turn"

    elif table.stage == "turn":
        # RIVER = 1
        if len(table.deck) < 1:
            finish_hand(table)
            return

        table.community.append(table.deck.pop())
        table.stage = "river"

    elif table.stage == "river":
        finish_hand(table)
        return

    table.current_index = first_to_act_postflop(table)

    if table.current_index is None:
        finish_hand(table)


def prepare_next_turn(table):
    if not table.started:
        return

    live = [
        p for p in table.players
        if not p["folded"] and not p["all_in"]
    ]

    non_folded = [
        p for p in table.players
        if not p["folded"]
    ]

    if len(non_folded) <= 1:
        finish_hand(table)
        return

    if len(live) == 0:
        # Everyone is all-in
        while len(table.community) < 5:
            if table.stage == "preflop":
                table.community.extend([
                    table.deck.pop(),
                    table.deck.pop(),
                    table.deck.pop()
                ])
                table.stage = "flop"
            elif table.stage == "flop":
                table.community.append(table.deck.pop())
                table.stage = "turn"
            elif table.stage == "turn":
                table.community.append(table.deck.pop())
                table.stage = "river"
            elif table.stage == "river":
                finish_hand(table)
                return

        finish_hand(table)
        return

    if all_equal_or_done(table):
        advance_stage(table)
        return

    n = len(table.players)

    for step in range(1, n + 1):
        idx = (table.current_index + step) % n
        p = table.players[idx]

        if not p["folded"] and not p["all_in"]:
            if p["bet"] < table.current_bet:
                table.current_index = idx
                return

    if all_equal_or_done(table):
        advance_stage(table)


# =========================================================
# PLAYER ACTION
# =========================================================

def do_action(table, player, action, amount=0):

    if not table.started:
        raise HTTPException(400, "Hand is not active")

    if table.players[table.current_index]["user_id"] != player["user_id"]:
        raise HTTPException(400, "Not your turn")

    if player["folded"] or player["all_in"]:
        raise HTTPException(400, "Invalid player")

    action = action.lower().strip()

    to_call = max(0, table.current_bet - player["bet"])

    # FOLD
    if action == "fold":
        player["folded"] = True
        table.last_action = f"{player['name']} folded"

    # CHECK
    elif action == "check":
        if to_call != 0:
            raise HTTPException(400, "Cannot check")

        table.last_action = f"{player['name']} checked"

    # CALL
    elif action == "call":
        if to_call <= 0:
            raise HTTPException(400, "Nothing to call")

        paid = min(to_call, player["chips"])

        player["chips"] -= paid
        player["bet"] += paid
        player["total_bet"] += paid
        table.pot += paid

        if player["chips"] == 0:
            player["all_in"] = True

        table.last_action = f"{player['name']} called"

    # RAISE
    elif action == "raise":
        requested = int(amount)

        minimum_total = table.current_bet + table.min_raise

        target = max(requested, minimum_total)

        if target <= table.current_bet:
            raise HTTPException(400, "Raise is too small")

        additional = target - player["bet"]

        if additional >= player["chips"]:
            additional = player["chips"]
            target = player["bet"] + additional

        if additional <= to_call:
            raise HTTPException(400, "Invalid raise")

        player["chips"] -= additional
        player["bet"] += additional
        player["total_bet"] += additional
        table.pot += additional

        old_bet = table.current_bet
        table.current_bet = player["bet"]
        table.min_raise = max(
            BIG_BLIND,
            table.current_bet - old_bet
        )

        if player["chips"] == 0:
            player["all_in"] = True

        table.last_action = f"{player['name']} raised"

    # ALL IN
    elif action == "allin":
        if player["chips"] <= 0:
            raise HTTPException(400, "No chips")

        old_bet = table.current_bet

        amount = player["chips"]

        player["chips"] = 0
        player["bet"] += amount
        player["total_bet"] += amount
        table.pot += amount
        player["all_in"] = True

        if player["bet"] > table.current_bet:
            table.current_bet = player["bet"]

            raise_size = player["bet"] - old_bet

            if raise_size > 0:
                table.min_raise = max(
                    table.min_raise,
                    raise_size
                )

        table.last_action = f"{player['name']} went all-in"

    else:
        raise HTTPException(400, "Unknown action")

    # Check whether only one remains
    remaining = [
        p for p in table.players
        if not p["folded"]
    ]

    if len(remaining) == 1:
        finish_hand(table)
        return

    # Find next eligible player
    n = len(table.players)

    next_idx = None

    for step in range(1, n + 1):
        idx = (table.current_index + step) % n
        p = table.players[idx]

        if not p["folded"] and not p["all_in"]:
            if p["bet"] < table.current_bet or p["bet"] == table.current_bet:
                next_idx = idx
                break

    if next_idx is None:
        advance_stage(table)
        return

    table.current_index = next_idx

    # If everyone has matched the bet, move street
    if all_equal_or_done(table):
        advance_stage(table)


# =========================================================
# POT / SHOWDOWN
# =========================================================

def calculate_pots(table):
    levels = sorted(
        set(
            p["total_bet"]
            for p in table.players
            if p["total_bet"] > 0
        )
    )

    pots = []
    previous = 0

    for level in levels:
        contributors = [
            p for p in table.players
            if p["total_bet"] >= level
        ]

        amount = (level - previous) * len(contributors)

        eligible = [
            p for p in contributors
            if not p["folded"]
        ]

        if amount > 0:
            pots.append({
                "amount": amount,
                "eligible": eligible
            })

        previous = level

    return pots


def finish_hand(table):
    if not table.started:
        return

    # If nobody folded and board is incomplete, complete board
    non_folded = [
        p for p in table.players
        if not p["folded"]
    ]

    while len(non_folded) > 1 and len(table.community) < 5:
        if len(table.community) == 0:
            table.community.extend([
                table.deck.pop(),
                table.deck.pop(),
                table.deck.pop()
            ])
        else:
            table.community.append(table.deck.pop())

    pots = calculate_pots(table)

    winners_all = []

    for pot in pots:
        eligible = pot["eligible"]

        if not eligible:
            continue

        scored = []

        for p in eligible:
            score = evaluate_seven(
                p["cards"] + table.community
            )
            scored.append((score, p))

        best_score = max(x[0] for x in scored)

        winners = [
            p for score, p in scored
            if score == best_score
        ]

        share = pot["amount"] // len(winners)
        remainder = pot["amount"] % len(winners)

        for i, winner in enumerate(winners):
            winner["chips"] += share

            if i < remainder:
                winner["chips"] += 1

            if winner["user_id"] not in winners_all:
                winners_all.append(winner["user_id"])

            winner["last_hand_name"] = hand_name(best_score)

    table.pot = 0

    table.winner = winners_all

    if winners_all:
        names = [
            p["name"]
            for p in table.players
            if p["user_id"] in winners_all
        ]
        table.message = "Winner: " + ", ".join(names)
    else:
        table.message = "Hand finished"

    table.started = False
    table.stage = "waiting"

    # Remove players with zero chips
    table.players = [
        p for p in table.players
        if p["chips"] > 0
    ]

    # Waiting players enter after hand
    seat_waiting_players(table)

    # Move dealer
    if table.players:
        table.dealer_index %= len(table.players)

        for _ in range(len(table.players)):
            table.dealer_index = (
                table.dealer_index + 1
            ) % len(table.players)

            if table.players[table.dealer_index]["chips"] > 0:
                break

    # Clear cards after a short delay
    for p in table.players:
        p["bet"] = 0
        p["total_bet"] = 0


# =========================================================
# BOT ENGINE
# =========================================================

def bot_strength(player, table):
    if len(player["cards"]) < 2:
        return 0.2

    score = evaluate_five(player["cards"] + table.community[:3])

    category = score[0]

    base = {
        0: 0.20,
        1: 0.42,
        2: 0.58,
        3: 0.72,
        4: 0.80,
        5: 0.84,
        6: 0.94,
        7: 0.98,
        8: 1.00
    }.get(category, 0.3)

    # Preflop use hole cards
    if len(table.community) == 0:
        a = card_value(player["cards"][0])
        b = card_value(player["cards"][1])

        if a == b:
            base = 0.55 + max(a - 2, 0) / 30

        if a >= 11 and b >= 11:
            base = 0.75

        if abs(a - b) <= 2:
            base += 0.08

    return min(base, 1.0)


async def bot_turn(table, player):

    await asyncio.sleep(random.uniform(0.8, 1.8))

    if not table.started:
        return

    if table.current_index >= len(table.players):
        return

    if table.players[table.current_index]["user_id"] != player["user_id"]:
        return

    if player["folded"] or player["all_in"]:
        return

    strength = bot_strength(player, table)

    to_call = max(
        0,
        table.current_bet - player["bet"]
    )

    # Strong hand
    if strength >= 0.75:
        if to_call == 0:
            action = "raise"
            amount = table.current_bet + (
                table.min_raise * random.choice([1, 1, 2])
            )

            amount = min(
                amount,
                player["bet"] + player["chips"]
            )

            if amount <= table.current_bet:
                action = "check"
                amount = 0

        else:
            if random.random() < 0.55:
                action = "raise"
                amount = table.current_bet + table.min_raise
                amount = min(
                    amount,
                    player["bet"] + player["chips"]
                )

                if amount <= table.current_bet:
                    action = "call"
                    amount = 0
            else:
                action = "call"
                amount = 0

    # Medium
    elif strength >= 0.45:
        if to_call == 0:
            action = random.choice(
                ["check", "check", "raise"]
            )
            amount = (
                table.current_bet + table.min_raise
                if action == "raise"
                else 0
            )
        else:
            if to_call <= BIG_BLIND * 2:
                action = "call"
                amount = 0
            else:
                action = random.choice(
                    ["call", "fold"]
                )
                amount = 0

    # Weak
    else:
        if to_call == 0:
            action = "check"
            amount = 0
        else:
            if random.random() < 0.20:
                action = "call"
            else:
                action = "fold"
            amount = 0

    try:
        do_action(
            table,
            player,
            action,
            amount
        )
    except Exception:
        try:
            if to_call == 0:
                do_action(
                    table,
                    player,
                    "check",
                    0
                )
            else:
                do_action(
                    table,
                    player,
                    "fold",
                    0
                )
        except Exception:
            pass


async def table_loop(table):

    while True:
        try:
            # Start new hand automatically
            if not table.started:
                seat_waiting_players(table)

                if can_start(table):
                    start_hand(table)

            if table.started:
                if table.current_index < len(table.players):
                    p = table.players[table.current_index]

                    if p["is_bot"]:
                        await bot_turn(table, p)
                    else:
                        await asyncio.sleep(0.25)
                else:
                    await asyncio.sleep(0.25)
            else:
                await asyncio.sleep(1)

        except Exception as e:
            table.message = "Table ready"
            await asyncio.sleep(1)


@app.on_event("startup")
async def startup():
    for table in tables.values():
        table.bot_task = asyncio.create_task(
            table_loop(table)
        )


# =========================================================
# API
# =========================================================

@app.get("/")
def root():
    return {
        "status": "online",
        "message": "Texas Holdem Poker Backend",
        "tables": TABLE_COUNT
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/tables")
def get_tables():

    result = []

    for table in tables.values():

        result.append({
            "table_id": table.id,
            "players": len(table.players),
            "waiting": len(table.waiting),
            "max_players": MAX_SEATS,
            "started": table.started,
            "stage": table.stage,
            "pot": table.pot,
            "message": table.message
        })

    return {
        "success": True,
        "tables": result
    }


@app.post("/join")
def join(req: JoinRequest):

    if req.table_id not in tables:
        raise HTTPException(400, "Invalid table")

    table = tables[req.table_id]

    user_id = str(req.user_id)

    # Already in this table
    existing = find_player(table, user_id)

    if existing:
        return {
            "success": True,
            "status": "already_joined",
            "table_id": table.id
        }

    # Remove user from other tables
    for t in tables.values():

        t.players = [
            p for p in t.players
            if p["user_id"] != user_id
        ]

        t.waiting = [
            p for p in t.waiting
            if p["user_id"] != user_id
        ]

    player = {
        "user_id": user_id,
        "name": req.name[:20] or "Player",
        "chips": STARTING_CHIPS,
        "cards": [],
        "folded": False,
        "all_in": False,
        "bet": 0,
        "total_bet": 0,
        "is_bot": False,
        "last_hand_name": ""
    }

    # If hand is running, wait for next hand
    if table.started:
        if len(table.players) + len(table.waiting) >= MAX_SEATS:
            raise HTTPException(400, "Table is full")

        table.waiting.append(player)

        return {
            "success": True,
            "status": "waiting",
            "table_id": table.id,
            "message": "You will join the next hand"
        }

    # Seat available
    if len(table.players) >= MAX_SEATS:
        raise HTTPException(400, "Table is full")

    table.players.append(player)

    # Add 3 bots
    while len([
        p for p in table.players
        if p["is_bot"]
    ]) < BOT_COUNT:

        if len(table.players) >= MAX_SEATS:
            break

        used_names = {
            p["name"]
            for p in table.players
        }

        available = [
            n for n in BOT_NAMES
            if n not in used_names
        ]

        if not available:
            available = BOT_NAMES

        bot = {
            "user_id": f"bot_{table.id}_{random.randint(10000,99999)}",
            "name": random.choice(available),
            "chips": random.randint(750, 1500),
            "cards": [],
            "folded": False,
            "all_in": False,
            "bet": 0,
            "total_bet": 0,
            "is_bot": True,
            "last_hand_name": ""
        }

        table.players.append(bot)

    return {
        "success": True,
        "status": "seated",
        "table_id": table.id,
        "message": "Joined table"
    }


@app.post("/leave")
def leave(user_id: str, table_id: int):

    if table_id not in tables:
        raise HTTPException(400, "Invalid table")

    table = tables[table_id]

    table.players = [
        p for p in table.players
        if p["user_id"] != str(user_id)
    ]

    table.waiting = [
        p for p in table.waiting
        if p["user_id"] != str(user_id)
    ]

    return {"success": True}


@app.get("/game")
def game(
    table_id: int,
    user_id: Optional[str] = None
):

    if table_id not in tables:
        raise HTTPException(400, "Invalid table")

    table = tables[table_id]

    players = []

    for i, p in enumerate(table.players):

        is_me = (
            user_id is not None
            and p["user_id"] == str(user_id)
        )

        players.append({
            "seat": i,
            "user_id": p["user_id"],
            "name": p["name"],
            "chips": p["chips"],
            "bet": p["bet"],
            "folded": p["folded"],
            "all_in": p["all_in"],
            "is_bot": False if p["is_bot"] else False,
            "is_me": is_me,
            "is_turn": (
                table.started
                and i == table.current_index
            )
        })

    my_cards = []

    if user_id:
        me = find_seated_player(
            table,
            str(user_id)
        )

        if me:
            my_cards = me["cards"]

    return {
        "success": True,
        "table_id": table.id,
        "started": table.started,
        "stage": table.stage,
        "community_cards": table.community,
        "pot": table.pot,
        "current_player": (
            table.players[table.current_index]["user_id"]
            if table.started
            and table.current_index < len(table.players)
            else None
        ),
        "current_player_name": (
            table.players[table.current_index]["name"]
            if table.started
            and table.current_index < len(table.players)
            else None
        ),
        "current_bet": table.current_bet,
        "winner": table.winner,
        "message": table.message,
        "last_action": table.last_action,
        "players": players,
        "my_cards": my_cards,
        "waiting": any(
            p["user_id"] == str(user_id)
            for p in table.waiting
        ) if user_id else False,
        "waiting_count": len(table.waiting)
    }


@app.get("/my-cards")
def my_cards(
    table_id: int,
    user_id: str
):

    if table_id not in tables:
        raise HTTPException(400, "Invalid table")

    table = tables[table_id]

    p = find_seated_player(
        table,
        str(user_id)
    )

    if not p:
        return {
            "success": True,
            "cards": []
        }

    return {
        "success": True,
        "cards": p["cards"]
    }


@app.post("/action")
def action(req: ActionRequest):

    if req.table_id not in tables:
        raise HTTPException(400, "Invalid table")

    table = tables[req.table_id]

    p = find_seated_player(
        table,
        str(req.user_id)
    )

    if not p:
        raise HTTPException(400, "Player not found")

    do_action(
        table,
        p,
        req.action,
        req.amount
    )

    return {
        "success": True,
        "action": req.action
    }


@app.post("/reset")
def reset():

    global tables

    tables = {
        i: Table(i)
        for i in range(1, TABLE_COUNT + 1)
    }

    return {
        "success": True,
        "message": "All tables reset"
    }
