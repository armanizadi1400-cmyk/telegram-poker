from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import random
import asyncio
import time
from itertools import combinations

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
TABLE_COUNT = 5

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
# MODELS
# =========================================================

class JoinRequest(BaseModel):
    user_id: str
    name: str = "Player"
    table_id: int = 1


class ActionRequest(BaseModel):
    user_id: str
    action: str
    amount: int = 0
    table_id: int = 1


class ChatRequest(BaseModel):
    user_id: str
    message: str
    table_id: int = 1


# =========================================================
# GAME / TABLE CREATION
# =========================================================

def new_game():
    return {
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


def new_table(table_id):
    return {
        "id": table_id,
        "name": f"Table {table_id}",
        "players": {},
        "game": new_game(),
    }


tables = {
    i: new_table(i)
    for i in range(1, TABLE_COUNT + 1)
}


# =========================================================
# HELPERS
# =========================================================

def get_table(table_id):
    table = tables.get(int(table_id))

    if not table:
        raise HTTPException(
            status_code=404,
            detail="Table not found"
        )

    return table


def seated_players(table):
    return sorted(
        [
            p
            for p in table["players"].values()
            if p.get("seat") is not None
        ],
        key=lambda x: x["seat"]
    )


def real_players(table):
    return [
        p
        for p in seated_players(table)
        if not p.get("is_bot", False)
    ]


def bot_players(table):
    return [
        p
        for p in seated_players(table)
        if p.get("is_bot", False)
    ]


def get_player(table, user_id):
    return table["players"].get(str(user_id))


def available_seat(table):
    used = {
        p["seat"]
        for p in seated_players(table)
    }

    for seat in range(MAX_SEATS):
        if seat not in used:
            return seat

    return None


def next_seat(table, seat):
    occupied = {
        p["seat"]
        for p in seated_players(table)
    }

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
# CARDS
# =========================================================

def make_deck():
    return [
        r + s
        for r in RANKS
        for s in SUITS
    ]


def card_text(card):
    if not card:
        return card

    return card[0] + SUIT_SYMBOLS.get(
        card[1],
        card[1]
    )


def cards_text(cards):
    return [
        card_text(card)
        for card in cards
    ]


def rank_value(card):
    return RANKS.index(card[0]) + 2


# =========================================================
# POT
# =========================================================

def calculate_pot(table):
    return sum(
        p.get("total_bet", 0)
        for p in seated_players(table)
    )


def update_pot(table):
    table["game"]["pot"] = calculate_pot(table)


# =========================================================
# PUBLIC GAME STATE
# =========================================================

def public_game_state(table, user_id=None):

    game = table["game"]

    public_players = []

    for p in seated_players(table):

        public_players.append({
            "user_id": p["user_id"],
            "name": p["name"],
            "chips": p["chips"],
            "seat": p["seat"],
            "bet": p["bet"],
            "total_bet": p["total_bet"],
            "folded": p["folded"],
            "all_in": p["all_in"],
            "is_bot": bool(p.get("is_bot", False)),
            "is_turn": (
                p["user_id"]
                == game["current_player"]
            ),
            "cards_count": len(p["cards"]),
        })

    my_cards = []

    if user_id:

        player = get_player(
            table,
            user_id
        )

        if player:
            my_cards = cards_text(
                player["cards"]
            )

    turn_remaining = 0

    if (
        game["started"]
        and game["current_player"]
        and game["turn_started_at"]
    ):

        elapsed = (
            time.time()
            - game["turn_started_at"]
        )

        turn_remaining = max(
            0,
            TURN_SECONDS - int(elapsed)
        )

    current_player_name = None
    current_player_is_bot = False

    if game["current_player"]:

        current = table["players"].get(
            game["current_player"]
        )

        if current:

            current_player_name = current["name"]

            current_player_is_bot = bool(
                current.get("is_bot", False)
            )

    return {
        "success": True,

        "table_id": table["id"],
        "table_name": table["name"],

        "started": game["started"],
        "stage": game["stage"],

        "community_cards": cards_text(
            game["community_cards"]
        ),

        "pot": game["pot"],

        "current_player":
            game["current_player"],

        "current_player_name":
            current_player_name,

        "current_player_is_bot":
            current_player_is_bot,

        "current_bet":
            game["current_bet"],

        "winner":
            game["winner"],

        "message":
            game["message"],

        "hand_number":
            game["hand_number"],

        "turn_started_at":
            game["turn_started_at"],

        "turn_seconds":
            TURN_SECONDS,

        "turn_remaining":
            turn_remaining,

        "players":
            public_players,

        "my_cards":
            my_cards,
    }


# =========================================================
# TABLE LIST
# =========================================================

def public_tables():

    result = []

    for table in tables.values():

        players_count = len(
            seated_players(table)
        )

        game = table["game"]

        if game["started"]:
            status = "PLAYING"

        elif players_count >= MAX_SEATS:
            status = "FULL"

        else:
            status = "OPEN"

        result.append({
            "id": table["id"],
            "name": table["name"],
            "players": players_count,
            "max_players": MAX_SEATS,
            "status": status,
            "pot": game["pot"],
            "stage": game["stage"],
            "small_blind": SMALL_BLIND,
            "big_blind": BIG_BLIND,
        })

    return result


# =========================================================
# DEAL
# =========================================================

def deal_card(table):

    deck = table["game"]["deck"]

    if not deck:

        raise HTTPException(
            status_code=400,
            detail="No cards left"
        )

    return deck.pop()


def deal_hole_cards(table):

    for p in seated_players(table):
        p["cards"] = []

    for _ in range(2):

        for p in seated_players(table):

            p["cards"].append(
                deal_card(table)
            )


# =========================================================
# BLINDS
# =========================================================

def post_blind(player, amount):

    actual = min(
        amount,
        player["chips"]
    )

    player["chips"] -= actual
    player["bet"] += actual
    player["total_bet"] += actual

    if player["chips"] == 0:
        player["all_in"] = True

    return actual


# =========================================================
# TURN HELPERS
# =========================================================

def actionable_players(table):

    return [
        p
        for p in seated_players(table)
        if player_can_act(p)
    ]


def next_actionable_from_seat(
    table,
    seat
):

    occupied = seated_players(table)

    if not occupied:
        return None

    for i in range(1, MAX_SEATS + 1):

        candidate_seat = (
            seat + i
        ) % MAX_SEATS

        for p in occupied:

            if (
                p["seat"] == candidate_seat
                and player_can_act(p)
            ):
                return p

    return None


def all_active_have_equal_bet(table):

    game = table["game"]

    active = [
        p
        for p in seated_players(table)
        if (
            not p["folded"]
            and not p["all_in"]
        )
    ]

    if not active:
        return True

    return all(
        p["bet"] == game["current_bet"]
        for p in active
    )


def only_one_active_player(table):

    active = [
        p
        for p in seated_players(table)
        if not p["folded"]
    ]

    return len(active) == 1


# =========================================================
# HAND EVALUATION
# =========================================================

def evaluate_five(cards):

    values = sorted(
        [
            rank_value(c)
            for c in cards
        ],
        reverse=True
    )

    suits = [
        c[1]
        for c in cards
    ]

    counts = {}

    for v in values:
        counts[v] = (
            counts.get(v, 0)
            + 1
        )

    unique_values = sorted(
        set(values),
        reverse=True
    )

    if 14 in unique_values:
        unique_values.append(1)

    straight_high = None

    for i in range(
        len(unique_values) - 4
    ):

        seq = unique_values[
            i:i + 5
        ]

        if seq[0] - seq[4] == 4:

            straight_high = seq[0]
            break

    flush = (
        len(set(suits)) == 1
    )

    if flush and straight_high:
        return (
            8,
            straight_high
        )

    four = [
        v
        for v, c in counts.items()
        if c == 4
    ]

    if four:

        kicker = max(
            v
            for v in values
            if v != four[0]
        )

        return (
            7,
            four[0],
            kicker
        )

    trips = sorted(
        [
            v
            for v, c in counts.items()
            if c == 3
        ],
        reverse=True
    )

    pairs = sorted(
        [
            v
            for v, c in counts.items()
            if c == 2
        ],
        reverse=True
    )

    if trips and (
        len(trips) >= 2
        or pairs
    ):

        trip = trips[0]

        if len(trips) >= 2:
            pair = trips[1]
        else:
            pair = pairs[0]

        return (
            6,
            trip,
            pair
        )

    if flush:
        return (
            5,
            *values
        )

    if straight_high:
        return (
            4,
            straight_high
        )

    if trips:

        kickers = sorted(
            [
                v
                for v in values
                if v != trips[0]
            ],
            reverse=True
        )[:2]

        return (
            3,
            trips[0],
            *kickers
        )

    if len(pairs) >= 2:

        pair1 = pairs[0]
        pair2 = pairs[1]

        kicker = max(
            v
            for v in values
            if (
                v != pair1
                and v != pair2
            )
        )

        return (
            2,
            pair1,
            pair2,
            kicker
        )

    if len(pairs) == 1:

        pair = pairs[0]

        kickers = sorted(
            [
                v
                for v in values
                if v != pair
            ],
            reverse=True
        )[:3]

        return (
            1,
            pair,
            *kickers
        )

    return (
        0,
        *values
    )


def best_hand(cards):

    if len(cards) < 5:

        values = sorted(
            [
                rank_value(c)
                for c in cards
            ],
            reverse=True
        )

        return (
            0,
            *values
        )

    best = None

    for combo in combinations(
        cards,
        5
    ):

        score = evaluate_five(combo)

        if (
            best is None
            or score > best
        ):
            best = score

    return best


# =========================================================
# FINISH HAND
# =========================================================

def finish_hand(table):

    game = table["game"]

    active = [
        p
        for p in seated_players(table)
        if not p["folded"]
    ]

    if not active:

        game["winner"] = None
        game["started"] = False
        game["stage"] = "waiting"
        game["current_player"] = None
        game["turn_started_at"] = None

        return

    prize = calculate_pot(table)

    if len(active) == 1:

        winner = active[0]

        winner["chips"] += prize

        game["winner"] = (
            winner["user_id"]
        )

        game["message"] = (
            f"{winner['name']} "
            f"wins {prize} chips"
        )

    else:

        scored = []

        for p in active:

            cards = (
                p["cards"]
                +
                game["community_cards"]
            )

            score = best_hand(cards)

            scored.append(
                (score, p)
            )

        best_score = max(
            score
            for score, _ in scored
        )

        winners = [
            p
            for score, p in scored
            if score == best_score
        ]

        share = (
            prize // len(winners)
        )

        remainder = (
            prize % len(winners)
        )

        for index, winner in enumerate(winners):

            winner["chips"] += share

            if index == 0:
                winner["chips"] += remainder

        names = ", ".join(
            w["name"]
            for w in winners
        )

        game["winner"] = (
            winners[0]["user_id"]
        )

        game["message"] = (
            f"{names} "
            f"wins {prize} chips"
        )

    update_pot(table)

    game["started"] = False
    game["current_player"] = None
    game["turn_started_at"] = None


# =========================================================
# ADVANCE STAGE
# =========================================================

def advance_stage(table):

    game = table["game"]

    game["acted_players"] = set()

    for p in seated_players(table):
        p["bet"] = 0

    game["current_bet"] = 0

    if game["stage"] == "preflop":

        game["community_cards"] = [
            deal_card(table),
            deal_card(table),
            deal_card(table),
        ]

        game["stage"] = "flop"

    elif game["stage"] == "flop":

        game["community_cards"].append(
            deal_card(table)
        )

        game["stage"] = "turn"

    elif game["stage"] == "turn":

        game["community_cards"].append(
            deal_card(table)
        )

        game["stage"] = "river"

    elif game["stage"] == "river":

        finish_hand(table)
        return

    dealer = game["dealer_index"]

    next_player = next_actionable_from_seat(
        table,
        dealer
    )

    if next_player:

        game["current_player"] = (
            next_player["user_id"]
        )

        game["turn_started_at"] = time.time()

    else:

        finish_hand(table)

    update_pot(table)


# =========================================================
# ACTION
# =========================================================

def perform_action(
    table,
    user_id,
    action,
    amount=0
):

    game = table["game"]

    player = get_player(
        table,
        user_id
    )

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

    action = (
        action
        .lower()
        .strip()
    )

    if action == "fold":

        player["folded"] = True

        game["acted_players"].add(
            user_id
        )

        if only_one_active_player(table):

            finish_hand(table)
            return

    elif action == "check":

        if player["bet"] != game["current_bet"]:

            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot check. "
                    "You must call or raise."
                )
            )

        game["acted_players"].add(
            user_id
        )

    elif action == "call":

        needed = (
            game["current_bet"]
            - player["bet"]
        )

        if needed > 0:

            actual = min(
                needed,
                player["chips"]
            )

            player["chips"] -= actual
            player["bet"] += actual
            player["total_bet"] += actual

            if player["chips"] == 0:
                player["all_in"] = True

        game["acted_players"].add(
            user_id
        )

    elif action == "raise":

        requested = int(amount)

        if requested <= game["current_bet"]:

            raise HTTPException(
                status_code=400,
                detail=(
                    "Raise must be greater "
                    "than current bet"
                )
            )

        additional = (
            requested
            - player["bet"]
        )

        additional = min(
            additional,
            player["chips"]
        )

        if additional <= 0:

            raise HTTPException(
                status_code=400,
                detail="Invalid raise"
            )

        player["chips"] -= additional
        player["bet"] += additional
        player["total_bet"] += additional

        game["current_bet"] = player["bet"]

        if player["chips"] == 0:
            player["all_in"] = True

        game["acted_players"] = {
            user_id
        }

    elif action in ("allin", "all-in"):

        additional = player["chips"]

        player["chips"] = 0
        player["bet"] += additional
        player["total_bet"] += additional
        player["all_in"] = True

        if player["bet"] > game["current_bet"]:

            game["current_bet"] = player["bet"]

            game["acted_players"] = {
                user_id
            }

        else:

            game["acted_players"].add(
                user_id
            )

    else:

        raise HTTPException(
            status_code=400,
            detail="Invalid action"
        )

    update_pot(table)

    if only_one_active_player(table):

        finish_hand(table)
        return

    if all_active_have_equal_bet(table):

        advance_stage(table)
        return

    next_player = next_actionable_from_seat(
        table,
        player["seat"]
    )

    if next_player:

        game["current_player"] = (
            next_player["user_id"]
        )

        game["turn_started_at"] = time.time()

    else:

        advance_stage(table)


# =========================================================
# BOTS
# =========================================================

def create_bot(table):

    seat = available_seat(table)

    if seat is None:
        return None

    existing_names = {
        p["name"]
        for p in seated_players(table)
    }

    available_names = [
        name
        for name in BOT_NAMES
        if name not in existing_names
    ]

    if available_names:
        name = random.choice(
            available_names
        )
    else:
        name = f"Bot {seat + 1}"

    bot_id = (
        f"bot_{table['id']}_"
        f"{seat}_"
        f"{random.randint(1000, 9999)}"
    )

    table["players"][bot_id] = {

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

    return table["players"][bot_id]


def add_bots_until(table, count=MAX_SEATS):

    while len(seated_players(table)) < count:

        bot = create_bot(table)

        if bot is None:
            break


def bot_decision(table, bot):

    game = table["game"]

    if not game["started"]:
        return

    if game["current_player"] != bot["user_id"]:
        return

    if not player_can_act(bot):
        return

    to_call = (
        game["current_bet"]
        - bot["bet"]
    )

    strength = random.random()

    if strength < 0.12:

        if to_call == 0:
            action = "check"
        else:
            action = "fold"

        amount = 0

    elif strength < 0.72:

        if to_call == 0:
            action = "check"
        else:
            action = "call"

        amount = 0

    elif strength < 0.93:

        raise_to = max(
            game["current_bet"] + BIG_BLIND,
            bot["bet"] + BIG_BLIND
        )

        max_bet = (
            bot["bet"]
            + bot["chips"]
        )

        amount = min(
            raise_to,
            max_bet
        )

        if amount <= game["current_bet"]:
            action = "call"
            amount = 0
        else:
            action = "raise"

    else:

        action = "allin"
        amount = 0

    perform_action(
        table,
        bot["user_id"],
        action,
        amount
    )


async def run_bot_turn(table):

    await asyncio.sleep(
        random.uniform(0.8, 1.8)
    )

    if not table["game"]["started"]:
        return

    current_id = (
        table["game"]["current_player"]
    )

    if not current_id:
        return

    player = table["players"].get(
        current_id
    )

    if not player:
        return

    if not player.get("is_bot", False):
        return

    try:

        bot_decision(
            table,
            player
        )

    except Exception:

        try:

            if (
                table["game"]["current_player"]
                == player["user_id"]
            ):

                if (
                    player["bet"]
                    ==
                    table["game"]["current_bet"]
                ):

                    perform_action(
                        table,
                        player["user_id"],
                        "check"
                    )

                else:

                    perform_action(
                        table,
                        player["user_id"],
                        "call"
                    )

        except Exception:
            pass

    await schedule_bot_if_needed(table)


async def schedule_bot_if_needed(table):

    if not table["game"]["started"]:
        return

    current_id = (
        table["game"]["current_player"]
    )

    if not current_id:
        return

    player = table["players"].get(
        current_id
    )

    if (
        player
        and player.get("is_bot", False)
    ):

        asyncio.create_task(
            run_bot_turn(table)
        )


# =========================================================
# START NEW HAND
# =========================================================

def start_new_hand(table):

    seated = seated_players(table)

    if len(seated) < 2:

        raise HTTPException(
            status_code=400,
            detail=(
                "At least 2 players "
                "are required"
            )
        )

    game = table["game"]

    game["started"] = True
    game["stage"] = "preflop"

    game["deck"] = make_deck()

    random.shuffle(
        game["deck"]
    )

    game["community_cards"] = []
    game["pot"] = 0
    game["current_bet"] = 0
    game["acted_players"] = set()
    game["winner"] = None
    game["message"] = ""
    game["turn_started_at"] = None
    game["hand_number"] += 1

    for p in seated:

        p["folded"] = False
        p["all_in"] = False
        p["bet"] = 0
        p["total_bet"] = 0
        p["cards"] = []

        # If a player ran out of chips,
        # give a fresh stack for the next hand.
        if p["chips"] <= 0:
            p["chips"] = STARTING_CHIPS

    occupied_seats = [
        p["seat"]
        for p in seated
    ]

    if game["dealer_index"] not in occupied_seats:

        game["dealer_index"] = (
            occupied_seats[0]
        )

    else:

        nxt = next_seat(
            table,
            game["dealer_index"]
        )

        if nxt is not None:
            game["dealer_index"] = nxt

    dealer = game["dealer_index"]

    if len(seated) == 2:

        sb_seat = dealer

        bb_seat = next_seat(
            table,
            dealer
        )

    else:

        sb_seat = next_seat(
            table,
            dealer
        )

        bb_seat = next_seat(
            table,
            sb_seat
        )

    sb_player = next(
        p
        for p in seated
        if p["seat"] == sb_seat
    )

    bb_player = next(
        p
        for p in seated
        if p["seat"] == bb_seat
    )

    post_blind(
        sb_player,
        SMALL_BLIND
    )

    post_blind(
        bb_player,
        BIG_BLIND
    )

    game["current_bet"] = BIG_BLIND

    deal_hole_cards(table)

    if len(seated) == 2:

        first = sb_player

    else:

        first = next_actionable_from_seat(
            table,
            bb_player["seat"]
        )

    if first:

        game["current_player"] = (
            first["user_id"]
        )

        game["turn_started_at"] = time.time()

    else:

        advance_stage(table)

    update_pot(table)


# =========================================================
# ROUTES
# =========================================================

@app.get("/")
def root():

    return {
        "status": "online",
        "message": "Poker backend is running",
        "version": "4.0",
        "tables": TABLE_COUNT,
        "max_seats": MAX_SEATS,
        "turn_seconds": TURN_SECONDS,
    }


@app.get("/health")
def health():

    return {
        "status": "ok",
        "version": "4.0"
    }


@app.get("/tables")
def get_tables():

    return {
        "success": True,
        "tables": public_tables()
    }


@app.get("/players")
def get_players(table_id: int = 1):

    table = get_table(table_id)

    return {
        "success": True,

        "table_id": table_id,

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
                "is_bot": bool(
                    p.get("is_bot", False)
                ),
                "is_turn": (
                    p["user_id"]
                    ==
                    table["game"]
                    ["current_player"]
                ),
                "cards_count": len(p["cards"]),
            }

            for p in seated_players(table)
        ],
    }


@app.get("/game")
def get_game(
    table_id: int = 1,
    user_id: Optional[str] = None
):

    table = get_table(table_id)

    return public_game_state(
        table,
        user_id
    )


@app.get("/my-cards")
def my_cards(
    user_id: str,
    table_id: int = 1
):

    table = get_table(table_id)

    player = get_player(
        table,
        user_id
    )

    if not player:

        raise HTTPException(
            status_code=404,
            detail="Player not found"
        )

    return {
        "success": True,
        "user_id": user_id,
        "cards": cards_text(
            player["cards"]
        ),
    }


# =========================================================
# JOIN
# =========================================================

@app.post("/join")
async def join(
    request: JoinRequest
):

    user_id = str(
        request.user_id
    )

    table = get_table(
        request.table_id
    )

    # Remove user from other tables
    for other in tables.values():

        if other["id"] == table["id"]:
            continue

        if user_id in other["players"]:

            if other["game"]["started"]:

                raise HTTPException(
                    status_code=400,
                    detail=(
                        "You are currently "
                        "playing another table"
                    )
                )

            other["players"].pop(
                user_id,
                None
            )

    existing = table["players"].get(
        user_id
    )

    if existing:

        # IMPORTANT:
        # Make sure bots exist even if
        # the user reloads the Mini App.
        if not table["game"]["started"]:

            add_bots_until(
                table,
                MAX_SEATS
            )

        return {
            "success": True,
            "message": "Already joined",
            "table_id": table["id"],
            "player": existing,
            "players": len(
                seated_players(table)
            ),
        }

    seat = available_seat(table)

    if seat is None:

        raise HTTPException(
            status_code=400,
            detail="Table is full"
        )

    table["players"][user_id] = {

        "user_id": user_id,

        "name":
            request.name or "Player",

        "chips":
            STARTING_CHIPS,

        "seat":
            seat,

        "cards":
            [],

        "folded":
            False,

        "all_in":
            False,

        "bet":
            0,

        "total_bet":
            0,

        "is_bot":
            False,
    }

    # =====================================================
    # IMPORTANT FIX:
    # Automatically fill the table with bots.
    # =====================================================

    add_bots_until(
        table,
        MAX_SEATS
    )

    return {

        "success": True,

        "message":
            "Player joined",

        "table_id":
            table["id"],

        "player":
            table["players"][user_id],

        "players":
            len(seated_players(table)),

        "bots":
            len(bot_players(table)),
    }


# =========================================================
# LEAVE
# =========================================================

@app.post("/leave")
async def leave(
    user_id: str,
    table_id: int = 1
):

    table = get_table(table_id)

    if table["game"]["started"]:

        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot leave during "
                "an active hand"
            )
        )

    table["players"].pop(
        str(user_id),
        None
    )

    return {
        "success": True,
        "message": "Left table"
    }


# =========================================================
# START
# =========================================================

@app.post("/start")
async def start_game(
    table_id: int = 1
):

    table = get_table(table_id)

    if table["game"]["started"]:

        return public_game_state(
            table
        )

    # Always make sure the table has
    # a full set of players.
    if len(seated_players(table)) < MAX_SEATS:

        add_bots_until(
            table,
            MAX_SEATS
        )

    if len(seated_players(table)) < 2:

        raise HTTPException(
            status_code=400,
            detail=(
                "At least 2 players "
                "are required"
            )
        )

    start_new_hand(table)

    await schedule_bot_if_needed(
        table
    )

    return public_game_state(
        table
    )


# =========================================================
# ACTION
# =========================================================

@app.post("/action")
async def action(
    request: ActionRequest
):

    table = get_table(
        request.table_id
    )

    perform_action(
        table,
        request.user_id,
        request.action,
        request.amount
    )

    await schedule_bot_if_needed(
        table
    )

    return public_game_state(
        table,
        request.user_id
    )


# =========================================================
# CHAT
# =========================================================

@app.post("/chat")
async def chat(
    request: ChatRequest
):

    table = get_table(
        request.table_id
    )

    player = get_player(
        table,
        request.user_id
    )

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


# =========================================================
# RESET
# =========================================================

@app.post("/reset")
async def reset(
    table_id: int = 1
):

    table = get_table(table_id)

    table["players"].clear()
    table["game"] = new_game()

    return {
        "success": True,
        "message": "Table reset"
    }


# =========================================================
# ADD BOTS
# =========================================================

@app.post("/add-bots")
async def add_bots(
    table_id: int = 1
):

    table = get_table(table_id)

    add_bots_until(
        table,
        MAX_SEATS
    )

    return {

        "success": True,

        "message":
            "Bots added",

        "players": [

            {
                "user_id":
                    p["user_id"],

                "name":
                    p["name"],

                "seat":
                    p["seat"],

                "is_bot":
                    bool(
                        p.get(
                            "is_bot",
                            False
                        )
                    ),
            }

            for p in seated_players(table)
        ],
    }


# =========================================================
# TIMER
# =========================================================

async def timer_loop():

    while True:

        await asyncio.sleep(1)

        for table in tables.values():

            game = table["game"]

            if not game["started"]:
                continue

            current_id = (
                game["current_player"]
            )

            if not current_id:
                continue

            player = table["players"].get(
                current_id
            )

            if not player:
                continue

            # Bots are handled separately.
            if player.get("is_bot", False):
                continue

            started_at = (
                game["turn_started_at"]
            )

            if not started_at:

                game["turn_started_at"] = (
                    time.time()
                )

                continue

            elapsed = (
                time.time()
                - started_at
            )

            if elapsed >= TURN_SECONDS:

                try:

                    # If player can check,
                    # automatically check.
                    if (
                        player["bet"]
                        ==
                        game["current_bet"]
                    ):

                        perform_action(
                            table,
                            current_id,
                            "check"
                        )

                    # Otherwise fold.
                    else:

                        perform_action(
                            table,
                            current_id,
                            "fold"
                        )

                    await schedule_bot_if_needed(
                        table
                    )

                except Exception:
                    pass


# =========================================================
# STARTUP
# =========================================================

@app.on_event("startup")
async def startup_event():

    asyncio.create_task(
        timer_loop()
    )
