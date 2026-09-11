from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import random
import asyncio
import itertools
from collections import Counter

app = FastAPI(title="Texas Holdem Poker Backend")

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
BOTS_PER_TABLE = 3

STARTING_CHIPS = 1000
SMALL_BLIND = 10
BIG_BLIND = 20

RANKS = "23456789TJQKA"
SUITS = ["♠", "♥", "♦", "♣"]

BOT_NAMES = [
    "Daniel", "Alex", "Michael", "Ryan",
    "David", "Kevin", "James", "Robert",
    "Thomas", "Jack", "William", "Chris",
    "Adam", "Sam", "John", "Mark",
    "Leo", "Nick"
]

# =========================================================
# CARD ENGINE
# =========================================================

def make_deck():
    deck = [rank + suit for rank in RANKS for suit in SUITS]
    random.shuffle(deck)
    return deck


def card_value(card):
    return RANKS.index(card[0]) + 2


def evaluate_five(cards):
    values = sorted(
        [card_value(c) for c in cards],
        reverse=True
    )

    suits = [c[1] for c in cards]
    counts = Counter(values)

    flush = len(set(suits)) == 1

    unique = sorted(set(values), reverse=True)

    if 14 in unique:
        unique.append(1)

    straight_high = None

    for i in range(len(unique) - 4):
        five = unique[i:i + 5]
        if five[0] - five[4] == 4:
            straight_high = five[0]
            break

    if flush and straight_high:
        return (8, straight_high)

    quads = [
        v for v, c in counts.items()
        if c == 4
    ]

    if quads:
        q = max(quads)
        kicker = max(v for v in values if v != q)
        return (7, q, kicker)

    trips = sorted(
        [v for v, c in counts.items() if c == 3],
        reverse=True
    )

    pairs = sorted(
        [v for v, c in counts.items() if c == 2],
        reverse=True
    )

    if trips and pairs:
        return (6, trips[0], pairs[0])

    if len(trips) >= 2:
        return (6, trips[0], trips[1])

    if flush:
        return (5, *values)

    if straight_high:
        return (4, straight_high)

    if trips:
        t = trips[0]
        kickers = sorted(
            [v for v in values if v != t],
            reverse=True
        )[:2]
        return (3, t, *kickers)

    if len(pairs) >= 2:
        p1, p2 = pairs[:2]
        kicker = max(
            v for v in values
            if v != p1 and v != p2
        )
        return (2, p1, p2, kicker)

    if len(pairs) == 1:
        p = pairs[0]
        kickers = sorted(
            [v for v in values if v != p],
            reverse=True
        )[:3]
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
# MODELS
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


# =========================================================
# TABLE
# =========================================================

class PokerTable:

    def __init__(self, table_id):

        self.id = table_id

        self.players = []
        self.waiting = []

        self.deck = []
        self.community = []

        self.pot = 0

        self.stage = "waiting"
        self.started = False

        self.dealer_index = 0
        self.current_index = 0

        self.current_bet = 0
        self.min_raise = BIG_BLIND

        # VERY IMPORTANT:
        # Players who have acted during THIS street.
        self.acted_this_street = set()

        self.hand_number = 0

        self.winner = None
        self.message = "Waiting for players"
        self.last_action = ""

        self.bot_task = None


tables = {
    i: PokerTable(i)
    for i in range(1, TABLE_COUNT + 1)
}


# =========================================================
# PLAYER HELPERS
# =========================================================

def find_player(table, user_id):

    uid = str(user_id)

    for p in table.players:
        if p["user_id"] == uid:
            return p

    for p in table.waiting:
        if p["user_id"] == uid:
            return p

    return None


def find_seated(table, user_id):

    uid = str(user_id)

    for p in table.players:
        if p["user_id"] == uid:
            return p

    return None


def active_players(table):

    return [
        p for p in table.players
        if not p["folded"]
    ]


def can_act(p):

    return (
        not p["folded"]
        and not p["all_in"]
        and p["chips"] > 0
    )


def reset_for_hand(p):

    p["cards"] = []
    p["folded"] = False
    p["all_in"] = False
    p["bet"] = 0
    p["total_bet"] = 0
    p["last_hand_name"] = ""


def seat_waiting(table):

    if table.started:
        return

    while (
        table.waiting
        and len(table.players) < MAX_SEATS
    ):
        table.players.append(
            table.waiting.pop(0)
        )


# =========================================================
# NEXT PLAYER
# =========================================================

def next_actor(table, start_index):

    if not table.players:
        return None

    count = len(table.players)

    for step in range(1, count + 1):

        idx = (
            start_index + step
        ) % count

        p = table.players[idx]

        if can_act(p):
            return idx

    return None


# =========================================================
# BLINDS
# =========================================================

def post_blind(player, amount):

    paid = min(
        amount,
        player["chips"]
    )

    player["chips"] -= paid
    player["bet"] += paid
    player["total_bet"] += paid

    if player["chips"] == 0:
        player["all_in"] = True

    return paid


# =========================================================
# START HAND
# =========================================================

def start_hand(table):

    seat_waiting(table)

    if len([
        p for p in table.players
        if p["chips"] > 0
    ]) < 2:

        table.started = False
        table.stage = "waiting"
        table.message = "Waiting for players"
        return False

    table.hand_number += 1

    table.started = True
    table.stage = "preflop"

    table.deck = make_deck()
    table.community = []

    table.pot = 0

    table.current_bet = 0
    table.min_raise = BIG_BLIND

    table.winner = None
    table.last_action = ""

    # VERY IMPORTANT
    table.acted_this_street = set()

    for p in table.players:
        reset_for_hand(p)

    if not table.players:
        return False

    table.dealer_index %= len(table.players)

    # =====================================================
    # DEAL HOLE CARDS
    # One card to every player, then second card.
    # =====================================================

    for _ in range(2):

        for p in table.players:

            if p["chips"] > 0:
                p["cards"].append(
                    table.deck.pop()
                )

    n = len(table.players)

    # Heads up
    if n == 2:

        sb_index = table.dealer_index
        bb_index = (
            table.dealer_index + 1
        ) % n

    # Normal game
    else:

        sb_index = (
            table.dealer_index + 1
        ) % n

        bb_index = (
            table.dealer_index + 2
        ) % n

    sb = table.players[sb_index]
    bb = table.players[bb_index]

    post_blind(
        sb,
        SMALL_BLIND
    )

    post_blind(
        bb,
        BIG_BLIND
    )

    table.current_bet = max(
        p["bet"]
        for p in table.players
    )

    table.pot = sum(
        p["bet"]
        for p in table.players
    )

    # =====================================================
    # PREFLOP FIRST PLAYER
    # =====================================================

    if n == 2:

        table.current_index = (
            table.dealer_index
        )

    else:

        table.current_index = (
            table.dealer_index + 3
        ) % n

    # If first player is not able to act,
    # find next available player.
    if not can_act(
        table.players[
            table.current_index
        ]
    ):

        nxt = next_actor(
            table,
            table.current_index
        )

        table.current_index = (
            nxt
            if nxt is not None
            else 0
        )

    table.message = "Pre-Flop"

    return True


# =========================================================
# STREET CONTROL
# =========================================================

def all_active_all_in(table):

    players = [
        p for p in table.players
        if not p["folded"]
    ]

    if len(players) <= 1:
        return True

    return all(
        p["all_in"]
        for p in players
    )


def betting_round_complete(table):

    active = [
        p for p in table.players
        if not p["folded"]
    ]

    if len(active) <= 1:
        return True

    # Everybody is all-in
    if all_active_all_in(table):
        return True

    # Every player who can act must have acted
    # AND must have matched the current bet.
    for p in active:

        if p["all_in"]:
            continue

        if p["user_id"] not in table.acted_this_street:
            return False

        if p["bet"] != table.current_bet:
            return False

    return True


def find_next_unfinished_actor(table):

    if not table.players:
        return None

    count = len(table.players)

    for step in range(1, count + 1):

        idx = (
            table.current_index + step
        ) % count

        p = table.players[idx]

        if p["folded"] or p["all_in"]:
            continue

        # Player needs to act if:
        # 1. has not acted this street
        # OR
        # 2. current bet is higher than their bet
        if (
            p["user_id"]
            not in table.acted_this_street
            or
            p["bet"] < table.current_bet
        ):
            return idx

    return None


# =========================================================
# MOVE TO NEXT STREET
# =========================================================

def advance_street(table):

    # =====================================================
    # NEVER advance directly from one street to another
    # unless the CURRENT betting round is actually complete.
    # =====================================================

    if not betting_round_complete(table):
        return False

    # If only one player remains
    active = [
        p for p in table.players
        if not p["folded"]
    ]

    if len(active) <= 1:

        finish_hand(table)
        return True

    # =====================================================
    # RESET STREET BETS
    # =====================================================

    for p in table.players:
        p["bet"] = 0

    table.current_bet = 0
    table.min_raise = BIG_BLIND

    table.acted_this_street = set()

    # =====================================================
    # PREFLOP -> FLOP
    # EXACTLY 3 CARDS
    # =====================================================

    if table.stage == "preflop":

        if len(table.deck) < 3:
            finish_hand(table)
            return True

        table.community = [
            table.deck.pop(),
            table.deck.pop(),
            table.deck.pop()
        ]

        table.stage = "flop"

    # =====================================================
    # FLOP -> TURN
    # EXACTLY 1 CARD
    # =====================================================

    elif table.stage == "flop":

        if len(table.deck) < 1:
            finish_hand(table)
            return True

        table.community.append(
            table.deck.pop()
        )

        table.stage = "turn"

    # =====================================================
    # TURN -> RIVER
    # EXACTLY 1 CARD
    # =====================================================

    elif table.stage == "turn":

        if len(table.deck) < 1:
            finish_hand(table)
            return True

        table.community.append(
            table.deck.pop()
        )

        table.stage = "river"

    # =====================================================
    # RIVER -> SHOWDOWN
    # =====================================================

    elif table.stage == "river":

        finish_hand(table)
        return True

    # =====================================================
    # NEW STREET STARTS
    #
    # FIRST PLAYER AFTER DEALER MUST ACT.
    # =====================================================

    nxt = None

    count = len(table.players)

    for step in range(1, count + 1):

        idx = (
            table.dealer_index + step
        ) % count

        p = table.players[idx]

        if can_act(p):
            nxt = idx
            break

    # Nobody can act -> showdown
    if nxt is None:

        finish_hand(table)
        return True

    table.current_index = nxt

    return True


# =========================================================
# PLAYER ACTION
# =========================================================

def do_action(
    table,
    player,
    action,
    amount=0
):

    if not table.started:
        raise HTTPException(
            400,
            "Hand is not active"
        )

    if (
        table.current_index
        >= len(table.players)
    ):
        raise HTTPException(
            400,
            "Invalid turn"
        )

    current = table.players[
        table.current_index
    ]

    if current["user_id"] != player["user_id"]:
        raise HTTPException(
            400,
            "Not your turn"
        )

    if player["folded"] or player["all_in"]:
        raise HTTPException(
            400,
            "Invalid player"
        )

    action = action.lower().strip()

    to_call = max(
        0,
        table.current_bet - player["bet"]
    )

    old_current_bet = table.current_bet

    # =====================================================
    # FOLD
    # =====================================================

    if action == "fold":

        player["folded"] = True

        table.last_action = (
            f"{player['name']} folded"
        )

    # =====================================================
    # CHECK
    # =====================================================

    elif action == "check":

        if to_call != 0:
            raise HTTPException(
                400,
                "Cannot check"
            )

        table.last_action = (
            f"{player['name']} checked"
        )

    # =====================================================
    # CALL
    # =====================================================

    elif action == "call":

        if to_call <= 0:

            raise HTTPException(
                400,
                "Nothing to call"
            )

        paid = min(
            to_call,
            player["chips"]
        )

        player["chips"] -= paid
        player["bet"] += paid
        player["total_bet"] += paid

        table.pot += paid

        if player["chips"] == 0:
            player["all_in"] = True

        table.last_action = (
            f"{player['name']} called"
        )

    # =====================================================
    # RAISE
    # =====================================================

    elif action == "raise":

        try:
            requested = int(amount)
        except:
            raise HTTPException(
                400,
                "Invalid raise"
            )

        minimum_total = (
            table.current_bet
            + table.min_raise
        )

        if requested < minimum_total:
            requested = minimum_total

        maximum_total = (
            player["bet"]
            + player["chips"]
        )

        target = min(
            requested,
            maximum_total
        )

        additional = (
            target - player["bet"]
        )

        if additional <= to_call:
            raise HTTPException(
                400,
                "Raise is too small"
            )

        player["chips"] -= additional
        player["bet"] += additional
        player["total_bet"] += additional

        table.pot += additional

        table.current_bet = player["bet"]

        raise_size = (
            table.current_bet
            - old_current_bet
        )

        table.min_raise = max(
            BIG_BLIND,
            raise_size
        )

        if player["chips"] == 0:
            player["all_in"] = True

        table.last_action = (
            f"{player['name']} raised"
        )

    # =====================================================
    # ALL IN
    # =====================================================

    elif action == "allin":

        if player["chips"] <= 0:
            raise HTTPException(
                400,
                "No chips"
            )

        amount_in = player["chips"]

        player["chips"] = 0

        player["bet"] += amount_in
        player["total_bet"] += amount_in

        table.pot += amount_in

        player["all_in"] = True

        if player["bet"] > table.current_bet:

            old_bet = table.current_bet

            table.current_bet = player["bet"]

            raise_size = (
                table.current_bet
                - old_bet
            )

            table.min_raise = max(
                BIG_BLIND,
                raise_size
            )

        table.last_action = (
            f"{player['name']} went all-in"
        )

    else:

        raise HTTPException(
            400,
            "Unknown action"
        )

    # =====================================================
    # IMPORTANT:
    # PLAYER HAS NOW ACTED THIS STREET
    # =====================================================

    table.acted_this_street.add(
        player["user_id"]
    )

    # =====================================================
    # CHECK FOR ONE PLAYER LEFT
    # =====================================================

    remaining = [
        p for p in table.players
        if not p["folded"]
    ]

    if len(remaining) <= 1:

        finish_hand(table)
        return

    # =====================================================
    # IF ALL PLAYERS ARE ALL-IN
    # RUNOUT IS ALLOWED.
    # =====================================================

    if all_active_all_in(table):

        while (
            len(table.community) < 5
        ):

            if len(table.community) == 0:

                table.community.extend([
                    table.deck.pop(),
                    table.deck.pop(),
                    table.deck.pop()
                ])

                table.stage = "flop"

            elif len(table.community) == 3:

                table.community.append(
                    table.deck.pop()
                )

                table.stage = "turn"

            elif len(table.community) == 4:

                table.community.append(
                    table.deck.pop()
                )

                table.stage = "river"

        finish_hand(table)
        return

    # =====================================================
    # FIND NEXT PLAYER
    # =====================================================

    nxt = find_next_unfinished_actor(
        table
    )

    if nxt is not None:

        table.current_index = nxt

        return

    # =====================================================
    # ONLY NOW CAN THE STREET CHANGE
    # =====================================================

    advance_street(table)


# =========================================================
# SHOWDOWN
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

        amount = (
            level - previous
        ) * len(contributors)

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

    active = [
        p for p in table.players
        if not p["folded"]
    ]

    # =====================================================
    # IF HAND DID NOT END BY FOLD,
    # COMPLETE BOARD TO 5 CARDS.
    # =====================================================

    if len(active) > 1:

        while len(table.community) < 5:

            if len(table.community) == 0:

                table.community.extend([
                    table.deck.pop(),
                    table.deck.pop(),
                    table.deck.pop()
                ])

                table.stage = "flop"

            elif len(table.community) == 3:

                table.community.append(
                    table.deck.pop()
                )

                table.stage = "turn"

            elif len(table.community) == 4:

                table.community.append(
                    table.deck.pop()
                )

                table.stage = "river"

    # =====================================================
    # SINGLE PLAYER
    # =====================================================

    if len(active) == 1:

        winner = active[0]

        winner["chips"] += table.pot

        table.winner = [
            winner["user_id"]
        ]

        table.message = (
            f"{winner['name']} wins"
        )

        table.pot = 0

    else:

        pots = calculate_pots(table)

        winners_ids = []

        for pot in pots:

            eligible = pot["eligible"]

            if not eligible:
                continue

            scored = []

            for p in eligible:

                score = evaluate_seven(
                    p["cards"]
                    + table.community
                )

                scored.append(
                    (score, p)
                )

            best_score = max(
                x[0]
                for x in scored
            )

            winners = [
                p
                for score, p in scored
                if score == best_score
            ]

            share = (
                pot["amount"]
                // len(winners)
            )

            remainder = (
                pot["amount"]
                % len(winners)
            )

            for i, winner in enumerate(
                winners
            ):

                winner["chips"] += share

                if i < remainder:
                    winner["chips"] += 1

                winner[
                    "last_hand_name"
                ] = hand_name(
                    best_score
                )

                if (
                    winner["user_id"]
                    not in winners_ids
                ):
                    winners_ids.append(
                        winner["user_id"]
                    )

        table.winner = winners_ids

        names = [
            p["name"]
            for p in table.players
            if p["user_id"] in winners_ids
        ]

        if names:

            table.message = (
                "Winner: "
                + ", ".join(names)
            )

        table.pot = 0

    # =====================================================
    # END HAND
    # =====================================================

    table.started = False
    table.stage = "waiting"

    table.acted_this_street = set()

    # Remove busted players
    table.players = [
        p for p in table.players
        if p["chips"] > 0
    ]

    # Add waiting players
    seat_waiting(table)

    # Dealer moves one seat
    if table.players:

        table.dealer_index %= len(
            table.players
        )

        table.dealer_index = (
            table.dealer_index + 1
        ) % len(table.players)

    # Reset street fields
    for p in table.players:

        p["bet"] = 0
        p["total_bet"] = 0
        p["cards"] = []


# =========================================================
# BOT
# =========================================================

def bot_strength(player, table):

    if len(player["cards"]) < 2:
        return 0.2

    if len(table.community) >= 3:

        score = evaluate_seven(
            player["cards"]
            + table.community
        )

        category = score[0]

        values = {
            0: 0.20,
            1: 0.42,
            2: 0.58,
            3: 0.70,
            4: 0.80,
            5: 0.84,
            6: 0.94,
            7: 0.98,
            8: 1.00
        }

        strength = values.get(
            category,
            0.3
        )

        return strength

    # PRE-FLOP
    a = card_value(
        player["cards"][0]
    )

    b = card_value(
        player["cards"][1]
    )

    strength = 0.25

    if a == b:
        strength = 0.65

    elif a >= 12 and b >= 12:
        strength = 0.70

    elif a >= 11 or b >= 11:
        strength = 0.48

    elif abs(a - b) <= 2:
        strength = 0.40

    return strength


async def bot_play(table, player):

    await asyncio.sleep(
        random.uniform(0.8, 1.7)
    )

    if not table.started:
        return

    if (
        table.current_index
        >= len(table.players)
    ):
        return

    current = table.players[
        table.current_index
    ]

    if current["user_id"] != player["user_id"]:
        return

    if player["folded"] or player["all_in"]:
        return

    strength = bot_strength(
        player,
        table
    )

    to_call = max(
        0,
        table.current_bet
        - player["bet"]
    )

    action = "fold"
    amount = 0

    # Strong
    if strength >= 0.75:

        if to_call == 0:

            if (
                random.random() < 0.55
                and player["chips"] > BIG_BLIND
            ):

                action = "raise"

                amount = (
                    table.current_bet
                    + table.min_raise
                    * random.choice([1, 1, 2])
                )

            else:

                action = "check"

        else:

            if (
                random.random() < 0.40
                and player["chips"] > to_call
            ):

                action = "raise"

                amount = (
                    table.current_bet
                    + table.min_raise
                )

            else:

                action = "call"

    # Medium
    elif strength >= 0.40:

        if to_call == 0:

            action = (
                "raise"
                if random.random() < 0.18
                else "check"
            )

            if action == "raise":

                amount = (
                    table.current_bet
                    + table.min_raise
                )

        else:

            if (
                to_call
                <= BIG_BLIND * 3
            ):

                action = "call"

            else:

                action = (
                    "call"
                    if random.random() < 0.35
                    else "fold"
                )

    # Weak
    else:

        if to_call == 0:

            action = "check"

        else:

            action = (
                "call"
                if random.random() < 0.12
                else "fold"
            )

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


# =========================================================
# TABLE LOOP
# =========================================================

async def table_loop(table):

    while True:

        try:

            if not table.started:

                seat_waiting(table)

                if len([
                    p for p in table.players
                    if p["chips"] > 0
                ]) >= 2:

                    start_hand(table)

            else:

                if (
                    table.current_index
                    < len(table.players)
                ):

                    player = table.players[
                        table.current_index
                    ]

                    if player["is_bot"]:

                        await bot_play(
                            table,
                            player
                        )

                    else:

                        # Human gets time to act.
                        await asyncio.sleep(
                            0.25
                        )

                else:

                    await asyncio.sleep(
                        0.25
                    )

        except Exception as e:

            table.message = (
                "Table ready"
            )

            await asyncio.sleep(1)


# =========================================================
# STARTUP
# =========================================================

@app.on_event("startup")
async def startup():

    for table in tables.values():

        table.bot_task = (
            asyncio.create_task(
                table_loop(table)
            )
        )


# =========================================================
# ROOT
# =========================================================

@app.get("/")
def root():

    return {
        "status": "online",
        "message": "Texas Holdem Poker Backend",
        "tables": 6
    }


@app.get("/health")
def health():

    return {
        "status": "ok"
    }


# =========================================================
# TABLES
# =========================================================

@app.get("/tables")
def get_tables():

    result = []

    for table in tables.values():

        result.append({
            "table_id": table.id,
            "players": len(
                table.players
            ),
            "waiting": len(
                table.waiting
            ),
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


# =========================================================
# JOIN
# =========================================================

@app.post("/join")
def join(req: JoinRequest):

    if req.table_id not in tables:

        raise HTTPException(
            400,
            "Invalid table"
        )

    table = tables[
        req.table_id
    ]

    uid = str(req.user_id)

    # Already seated
    if find_seated(table, uid):

        return {
            "success": True,
            "status": "already_joined",
            "table_id": table.id
        }

    # Already waiting
    if any(
        p["user_id"] == uid
        for p in table.waiting
    ):

        return {
            "success": True,
            "status": "waiting",
            "table_id": table.id
        }

    # Remove from other tables
    for t in tables.values():

        t.players = [
            p for p in t.players
            if p["user_id"] != uid
        ]

        t.waiting = [
            p for p in t.waiting
            if p["user_id"] != uid
        ]

    player = {

        "user_id": uid,

        "name": (
            req.name[:20]
            or "Player"
        ),

        "chips": STARTING_CHIPS,

        "cards": [],

        "folded": False,

        "all_in": False,

        "bet": 0,

        "total_bet": 0,

        "is_bot": False,

        "last_hand_name": ""
    }

    # If hand is running:
    # wait for next hand.
    if table.started:

        if (
            len(table.players)
            + len(table.waiting)
            >= MAX_SEATS
        ):

            raise HTTPException(
                400,
                "Table is full"
            )

        table.waiting.append(
            player
        )

        return {
            "success": True,
            "status": "waiting",
            "table_id": table.id
        }

    # Table full
    if len(table.players) >= MAX_SEATS:

        raise HTTPException(
            400,
            "Table is full"
        )

    # Add human
    table.players.append(
        player
    )

    # =====================================================
    # ADD EXACTLY 3 BOTS
    # =====================================================

    while len([
        p for p in table.players
        if p["is_bot"]
    ]) < BOTS_PER_TABLE:

        if len(table.players) >= MAX_SEATS:
            break

        used = {
            p["name"]
            for p in table.players
        }

        available = [
            name
            for name in BOT_NAMES
            if name not in used
        ]

        if not available:
            available = BOT_NAMES

        bot = {

            "user_id":
                f"bot_{table.id}_"
                f"{random.randint(10000,99999)}",

            "name":
                random.choice(
                    available
                ),

            "chips":
                random.randint(
                    700,
                    1500
                ),

            "cards": [],

            "folded": False,

            "all_in": False,

            "bet": 0,

            "total_bet": 0,

            "is_bot": True,

            "last_hand_name": ""
        }

        table.players.append(
            bot
        )

    return {
        "success": True,
        "status": "seated",
        "table_id": table.id
    }


# =========================================================
# LEAVE
# =========================================================

@app.post("/leave")
def leave(
    user_id: str,
    table_id: int
):

    if table_id not in tables:

        raise HTTPException(
            400,
            "Invalid table"
        )

    table = tables[
        table_id
    ]

    uid = str(user_id)

    table.players = [
        p for p in table.players
        if p["user_id"] != uid
    ]

    table.waiting = [
        p for p in table.waiting
        if p["user_id"] != uid
    ]

    return {
        "success": True
    }


# =========================================================
# GAME
# =========================================================

@app.get("/game")
def get_game(
    table_id: int,
    user_id: Optional[str] = None
):

    if table_id not in tables:

        raise HTTPException(
            400,
            "Invalid table"
        )

    table = tables[
        table_id
    ]

    result_players = []

    for i, p in enumerate(
        table.players
    ):

        result_players.append({

            "seat": i,

            "user_id":
                p["user_id"],

            "name":
                p["name"],

            "chips":
                p["chips"],

            "bet":
                p["bet"],

            "folded":
                p["folded"],

            "all_in":
                p["all_in"],

            "is_me":
                (
                    user_id is not None
                    and
                    p["user_id"]
                    == str(user_id)
                ),

            "is_turn":
                (
                    table.started
                    and
                    i
                    == table.current_index
                )
        })

    my_cards = []

    if user_id:

        me = find_seated(
            table,
            user_id
        )

        if me:

            my_cards = me[
                "cards"
            ]

    current_player = None
    current_player_name = None

    if (
        table.started
        and
        table.current_index
        < len(table.players)
    ):

        current = table.players[
            table.current_index
        ]

        current_player = (
            current["user_id"]
        )

        current_player_name = (
            current["name"]
        )

    waiting = False

    if user_id:

        waiting = any(
            p["user_id"]
            == str(user_id)
            for p in table.waiting
        )

    return {

        "success": True,

        "table_id":
            table.id,

        "started":
            table.started,

        "stage":
            table.stage,

        "community_cards":
            table.community,

        "pot":
            table.pot,

        "current_player":
            current_player,

        "current_player_name":
            current_player_name,

        "current_bet":
            table.current_bet,

        "winner":
            table.winner,

        "message":
            table.message,

        "last_action":
            table.last_action,

        "players":
            result_players,

        "my_cards":
            my_cards,

        "waiting":
            waiting,

        "waiting_count":
            len(table.waiting)
    }


# =========================================================
# MY CARDS
# =========================================================

@app.get("/my-cards")
def get_my_cards(
    table_id: int,
    user_id: str
):

    if table_id not in tables:

        raise HTTPException(
            400,
            "Invalid table"
        )

    table = tables[
        table_id
    ]

    player = find_seated(
        table,
        user_id
    )

    if not player:

        return {
            "success": True,
            "cards": []
        }

    return {
        "success": True,
        "cards": player[
            "cards"
        ]
    }


# =========================================================
# ACTION
# =========================================================

@app.post("/action")
def action(req: ActionRequest):

    if req.table_id not in tables:

        raise HTTPException(
            400,
            "Invalid table"
        )

    table = tables[
        req.table_id
    ]

    player = find_seated(
        table,
        req.user_id
    )

    if not player:

        raise HTTPException(
            400,
            "Player not found"
        )

    do_action(
        table,
        player,
        req.action,
        req.amount
    )

    return {
        "success": True,
        "action": req.action,
        "stage": table.stage,
        "community_cards":
            table.community,
        "pot":
            table.pot
    }


# =========================================================
# RESET
# =========================================================

@app.post("/reset")
def reset():

    global tables

    tables = {
        i: PokerTable(i)
        for i in range(1, TABLE_COUNT + 1)
    }

    return {
        "success": True,
        "message": "All tables reset"
    }
