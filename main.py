from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import random
import asyncio
import time

app = FastAPI(title="Telegram Poker 6 Tables")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
# CONFIG
# =========================================================

TABLE_COUNT = 6
MAX_SEATS = 6

STARTING_CHIPS = 1000
SMALL_BLIND = 10
BIG_BLIND = 20

TURN_SECONDS = 25

RANKS = list("23456789TJQKA")
SUITS = ["♠", "♥", "♦", "♣"]

TABLES = {}


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
# TABLE CREATION
# =========================================================

def create_deck():
    return [
        rank + suit
        for rank in RANKS
        for suit in SUITS
    ]


def new_table(table_id):

    return {
        "id": table_id,
        "name": f"Table {table_id}",

        "players": {},

        "game": {
            "started": False,
            "deck": [],
            "community_cards": [],
            "pot": 0,
            "stage": "waiting",
            "current_player": None,
            "current_bet": 0,
            "dealer_index": 0,
            "turn_started_at": None,
            "winner": None,
            "message": "",
            "hand_number": 0,
        },

        "chat": []
    }


for i in range(1, TABLE_COUNT + 1):
    TABLES[i] = new_table(i)


# =========================================================
# HELPERS
# =========================================================

def get_table(table_id):

    if table_id not in TABLES:
        return None

    return TABLES[table_id]


def bot_name(table_id, number):
    names = [
        "Alex",
        "Mike",
        "David",
        "Sam",
        "Jack",
        "Tom",
        "Daniel",
        "Chris",
        "Leo",
        "Ryan",
        "Adam",
        "Max"
    ]

    return names[
        ((table_id - 1) * 3 + number - 1)
        % len(names)
    ]


def add_bot(table, number):

    bot_id = f"bot_{table['id']}_{number}"

    if bot_id in table["players"]:
        return table["players"][bot_id]

    occupied = {
        p["seat"]
        for p in table["players"].values()
    }

    free_seats = [
        x for x in range(MAX_SEATS)
        if x not in occupied
    ]

    if not free_seats:
        return None

    player = {
        "user_id": bot_id,
        "name": bot_name(table["id"], number),
        "chips": STARTING_CHIPS,
        "cards": [],
        "folded": False,
        "all_in": False,
        "bet": 0,
        "total_bet": 0,
        "seat": free_seats[0],
        "is_bot": True
    }

    table["players"][bot_id] = player

    return player


def ensure_three_bots(table):

    bots = [
        p for p in table["players"].values()
        if p["is_bot"]
    ]

    number = 1

    while len(bots) < 3:

        bot = add_bot(
            table,
            number
        )

        if bot:
            bots.append(bot)

        number += 1


def get_active_players(table):

    return [
        p for p in table["players"].values()
        if not p["folded"] and p["chips"] > 0
    ]


def next_player(table, current_id):

    players = sorted(
        table["players"].values(),
        key=lambda x: x["seat"]
    )

    if not players:
        return None

    current_seat = None

    for p in players:
        if p["user_id"] == current_id:
            current_seat = p["seat"]
            break

    if current_seat is None:
        return players[0]

    for p in players:

        if p["seat"] > current_seat:
            if not p["folded"] and not p["all_in"]:
                return p

    for p in players:

        if not p["folded"] and not p["all_in"]:
            return p

    return None


def set_turn(table, player):

    game = table["game"]

    if player:

        game["current_player"] = player["user_id"]
        game["turn_started_at"] = time.time()

    else:

        game["current_player"] = None
        game["turn_started_at"] = None


def reset_player_for_hand(player):

    player["cards"] = []
    player["folded"] = False
    player["all_in"] = False
    player["bet"] = 0
    player["total_bet"] = 0


def deal_cards(table):

    deck = table["game"]["deck"]

    players = sorted(
        table["players"].values(),
        key=lambda x: x["seat"]
    )

    for player in players:

        if player["chips"] <= 0:
            player["chips"] = STARTING_CHIPS

        reset_player_for_hand(player)

        player["cards"] = [
            deck.pop(),
            deck.pop()
        ]


def post_blind(player, amount, game):

    actual = min(
        player["chips"],
        amount
    )

    player["chips"] -= actual
    player["bet"] += actual
    player["total_bet"] += actual

    game["pot"] += actual

    if player["chips"] == 0:
        player["all_in"] = True


def start_new_hand(table):

    ensure_three_bots(table)

    game = table["game"]

    if len(table["players"]) < 2:
        return {
            "success": False,
            "message": "Need at least 2 players"
        }

    game["started"] = True
    game["deck"] = create_deck()
    random.shuffle(game["deck"])

    game["community_cards"] = []
    game["pot"] = 0
    game["stage"] = "preflop"
    game["current_bet"] = BIG_BLIND
    game["winner"] = None
    game["message"] = ""
    game["hand_number"] += 1

    deal_cards(table)

    players = sorted(
        table["players"].values(),
        key=lambda x: x["seat"]
    )

    dealer_index = game["dealer_index"] % len(players)

    small_index = (dealer_index + 1) % len(players)
    big_index = (dealer_index + 2) % len(players)

    post_blind(
        players[small_index],
        SMALL_BLIND,
        game
    )

    post_blind(
        players[big_index],
        BIG_BLIND,
        game
    )

    game["dealer_index"] = (
        dealer_index + 1
    ) % len(players)

    first_player = next_player(
        table,
        players[big_index]["user_id"]
    )

    set_turn(
        table,
        first_player
    )

    return {
        "success": True,
        "started": True
    }


def deal_flop(table):

    game = table["game"]

    if len(game["community_cards"]) >= 3:
        return

    deck = game["deck"]

    if len(deck) < 3:
        return

    deck.pop()

    game["community_cards"].extend([
        deck.pop(),
        deck.pop(),
        deck.pop()
    ])

    game["stage"] = "flop"


def deal_turn(table):

    game = table["game"]

    if len(game["community_cards"]) >= 4:
        return

    deck = game["deck"]

    if not deck:
        return

    deck.pop()

    game["community_cards"].append(
        deck.pop()
    )

    game["stage"] = "turn"


def deal_river(table):

    game = table["game"]

    if len(game["community_cards"]) >= 5:
        return

    deck = game["deck"]

    if not deck:
        return

    deck.pop()

    game["community_cards"].append(
        deck.pop()
    )

    game["stage"] = "river"


def finish_hand(table):

    game = table["game"]

    active = [
        p for p in table["players"].values()
        if not p["folded"]
    ]

    if not active:
        return

    winner = random.choice(active)

    winner["chips"] += game["pot"]

    game["winner"] = winner["name"]

    game["message"] = (
        f"{winner['name']} wins "
        f"{game['pot']} chips"
    )

    game["pot"] = 0
    game["started"] = False
    game["current_player"] = None
    game["turn_started_at"] = None

    for p in table["players"].values():

        p["bet"] = 0
        p["total_bet"] = 0


def advance_stage(table):

    game = table["game"]

    if game["stage"] == "preflop":

        deal_flop(table)

    elif game["stage"] == "flop":

        deal_turn(table)

    elif game["stage"] == "turn":

        deal_river(table)

    elif game["stage"] == "river":

        finish_hand(table)
        return

    for p in table["players"].values():
        p["bet"] = 0

    game["current_bet"] = 0

    active = [
        p for p in table["players"].values()
        if not p["folded"]
        and not p["all_in"]
    ]

    if active:

        active.sort(
            key=lambda x: x["seat"]
        )

        set_turn(
            table,
            active[0]
        )


def all_bets_equal(table):

    active = [
        p for p in table["players"].values()
        if not p["folded"]
        and not p["all_in"]
    ]

    if len(active) <= 1:
        return True

    return all(
        p["bet"] == table["game"]["current_bet"]
        for p in active
    )


def action_call(table, player):

    game = table["game"]

    needed = max(
        0,
        game["current_bet"]
        -
        player["bet"]
    )

    actual = min(
        needed,
        player["chips"]
    )

    player["chips"] -= actual
    player["bet"] += actual
    player["total_bet"] += actual
    game["pot"] += actual

    if player["chips"] == 0:
        player["all_in"] = True


def action_raise(table, player, amount):

    game = table["game"]

    target = max(
        amount,
        game["current_bet"] + BIG_BLIND
    )

    needed = max(
        0,
        target - player["bet"]
    )

    actual = min(
        needed,
        player["chips"]
    )

    player["chips"] -= actual
    player["bet"] += actual
    player["total_bet"] += actual
    game["pot"] += actual

    game["current_bet"] = player["bet"]

    if player["chips"] == 0:
        player["all_in"] = True


def move_after_action(table, player):

    active = [
        p for p in table["players"].values()
        if not p["folded"]
    ]

    if len(active) == 1:

        finish_hand(table)
        return

    if all_bets_equal(table):

        all_ready = all(
            p["folded"]
            or p["all_in"]
            or p["bet"] == game_current_bet(table)
            for p in table["players"].values()
        )

        if all_ready:

            advance_stage(table)
            return

    nxt = next_player(
        table,
        player["user_id"]
    )

    set_turn(
        table,
        nxt
    )


def game_current_bet(table):

    return table["game"]["current_bet"]


# =========================================================
# GAME RESPONSE
# =========================================================

def game_response(
    table,
    user_id=None
):

    game = table["game"]

    now = time.time()

    remaining = TURN_SECONDS

    if game["turn_started_at"]:

        remaining = max(
            0,
            TURN_SECONDS -
            int(
                now -
                game["turn_started_at"]
            )
        )

    players = []

    for p in sorted(
        table["players"].values(),
        key=lambda x: x["seat"]
    ):

        players.append({
            "user_id": p["user_id"],
            "name": p["name"],
            "chips": p["chips"],
            "seat": p["seat"],
            "bet": p["bet"],
            "total_bet": p["total_bet"],
            "folded": p["folded"],
            "all_in": p["all_in"],
            "is_bot": p["is_bot"],
            "is_turn": (
                p["user_id"]
                ==
                game["current_player"]
            ),
            "cards_count": len(
                p["cards"]
            )
        })

    my_cards = []

    if user_id in table["players"]:

        my_cards = table["players"][
            user_id
        ]["cards"]

    return {
        "success": True,

        "table_id":
            table["id"],

        "table_name":
            table["name"],

        "started":
            game["started"],

        "stage":
            game["stage"],

        "community_cards":
            game["community_cards"],

        "pot":
            game["pot"],

        "current_player":
            game["current_player"],

        "current_player_name":
            (
                table["players"]
                .get(
                    game["current_player"],
                    {}
                )
                .get(
                    "name"
                )
            ),

        "current_player_is_bot":
            (
                table["players"]
                .get(
                    game["current_player"],
                    {}
                )
                .get(
                    "is_bot",
                    False
                )
            ),

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
            remaining,

        "players":
            players,

        "my_cards":
            my_cards
    }


# =========================================================
# ROOT
# =========================================================

@app.get("/")
def root():

    return {
        "status": "online",
        "message":
            "Telegram Poker 6 Tables",
        "tables":
            TABLE_COUNT
    }


@app.get("/health")
def health():

    return {
        "status": "ok",
        "tables":
            TABLE_COUNT
    }


# =========================================================
# TABLES
# =========================================================

@app.get("/tables")
def get_tables():

    result = []

    for table in TABLES.values():

        bots = sum(
            1
            for p in table["players"].values()
            if p["is_bot"]
        )

        humans = sum(
            1
            for p in table["players"].values()
            if not p["is_bot"]
        )

        result.append({
            "table_id":
                table["id"],

            "name":
                table["name"],

            "players":
                len(table["players"]),

            "humans":
                humans,

            "bots":
                bots,

            "started":
                table["game"]["started"]
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

    table = get_table(
        req.table_id
    )

    if not table:

        return {
            "success": False,
            "message": "Table not found"
        }

    # If user is already in another table,
    # remove them from that table.

    for other in TABLES.values():

        if req.user_id in other["players"]:

            if other["id"] != req.table_id:

                del other["players"][
                    req.user_id
                ]

    # Already joined

    if req.user_id in table["players"]:

        ensure_three_bots(table)

        return game_response(
            table,
            req.user_id
        )

    # Check free seats

    occupied = {
        p["seat"]
        for p in table["players"].values()
    }

    free = [
        x for x in range(MAX_SEATS)
        if x not in occupied
    ]

    if not free:

        return {
            "success": False,
            "message": "Table is full"
        }

    player = {
        "user_id":
            req.user_id,

        "name":
            req.name,

        "chips":
            STARTING_CHIPS,

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

        "seat":
            free[0],

        "is_bot":
            False
    }

    table["players"][
        req.user_id
    ] = player

    ensure_three_bots(
        table
    )

    return game_response(
        table,
        req.user_id
    )


# =========================================================
# LEAVE
# =========================================================

@app.post("/leave")
def leave(req: JoinRequest):

    table = get_table(
        req.table_id
    )

    if not table:
        return {
            "success": False
        }

    if req.user_id in table["players"]:

        player = table["players"][
            req.user_id
        ]

        if not player["is_bot"]:

            del table["players"][
                req.user_id
            ]

    return {
        "success": True
    }


# =========================================================
# PLAYERS
# =========================================================

@app.get("/players")
def players(
    table_id: int = 1
):

    table = get_table(
        table_id
    )

    if not table:

        return {
            "success": False,
            "players": []
        }

    return {
        "success": True,
        "players": [
            {
                "user_id":
                    p["user_id"],

                "name":
                    p["name"],

                "chips":
                    p["chips"],

                "seat":
                    p["seat"],

                "is_bot":
                    p["is_bot"],

                "is_turn":
                    (
                        p["user_id"]
                        ==
                        table["game"][
                            "current_player"
                        ]
                    )
            }

            for p in sorted(
                table["players"].values(),
                key=lambda x: x["seat"]
            )
        ]
    }


# =========================================================
# GAME
# =========================================================

@app.get("/game")
def get_game(
    table_id: int = 1,
    user_id: Optional[str] = None
):

    table = get_table(
        table_id
    )

    if not table:

        return {
            "success": False,
            "message": "Table not found"
        }

    return game_response(
        table,
        user_id
    )


# =========================================================
# MY CARDS
# =========================================================

@app.get("/my-cards")
def my_cards(
    user_id: str,
    table_id: int = 1
):

    table = get_table(
        table_id
    )

    if not table:

        return {
            "success": False,
            "cards": []
        }

    player = table["players"].get(
        user_id
    )

    if not player:

        return {
            "success": False,
            "cards": []
        }

    return {
        "success": True,
        "cards": player["cards"]
    }


# =========================================================
# START
# =========================================================

@app.post("/start")
def start(
    table_id: int = 1
):

    table = get_table(
        table_id
    )

    if not table:

        return {
            "success": False,
            "message": "Table not found"
        }

    ensure_three_bots(
        table
    )

    result = start_new_hand(
        table
    )

    return game_response(
        table
    ) | result


# =========================================================
# ACTION
# =========================================================

@app.post("/action")
def action(
    req: ActionRequest
):

    table = get_table(
        req.table_id
    )

    if not table:

        return {
            "success": False,
            "message": "Table not found"
        }

    game = table["game"]

    if not game["started"]:

        return {
            "success": False,
            "message": "Game not started"
        }

    if (
        game["current_player"]
        !=
        req.user_id
    ):

        return {
            "success": False,
            "message": "Not your turn"
        }

    player = table["players"].get(
        req.user_id
    )

    if not player:

        return {
            "success": False,
            "message": "Player not found"
        }

    action_name = (
        req.action.lower()
    )

    if action_name == "fold":

        player["folded"] = True

    elif action_name == "check":

        if (
            player["bet"]
            !=
            game["current_bet"]
        ):

            return {
                "success": False,
                "message":
                    "Cannot check"
            }

    elif action_name == "call":

        action_call(
            table,
            player
        )

    elif action_name == "raise":

        action_raise(
            table,
            player,
            req.amount
        )

    elif action_name == "allin":

        amount = player["chips"]

        target = (
            player["bet"]
            +
            amount
        )

        player["chips"] = 0
        player["bet"] = target
        player["total_bet"] += amount

        game["pot"] += amount

        player["all_in"] = True

        if target > game["current_bet"]:

            game["current_bet"] = target

    else:

        return {
            "success": False,
            "message":
                "Invalid action"
        }

    move_after_action(
        table,
        player
    )

    return game_response(
        table,
        req.user_id
    )


# =========================================================
# BOT ACTION
# =========================================================

def bot_action(table):

    game = table["game"]

    if not game["started"]:
        return

    user_id = game["current_player"]

    if not user_id:
        return

    player = table["players"].get(
        user_id
    )

    if not player:
        return

    if not player["is_bot"]:
        return

    if player["folded"]:
        return

    if player["all_in"]:
        return

    # Random but reasonable bot behavior

    roll = random.random()

    if roll < 0.10:

        player["folded"] = True

        move_after_action(
            table,
            player
        )

        return

    if roll < 0.65:

        action_call(
            table,
            player
        )

        move_after_action(
            table,
            player
        )

        return

    # Raise

    raise_to = max(
        game["current_bet"] +
        BIG_BLIND,
        BIG_BLIND * 2
    )

    action_raise(
        table,
        player,
        raise_to
    )

    move_after_action(
        table,
        player
    )


# =========================================================
# TIMER / BOT LOOP
# =========================================================

async def bot_loop():

    while True:

        try:

            for table in TABLES.values():

                game = table["game"]

                if not game["started"]:
                    continue

                current_id = (
                    game["current_player"]
                )

                player = (
                    table["players"]
                    .get(current_id)
                )

                if not player:
                    continue

                # Timeout

                if (
                    game["turn_started_at"]
                    and
                    time.time()
                    -
                    game["turn_started_at"]
                    >=
                    TURN_SECONDS
                ):

                    if player["is_bot"]:

                        bot_action(
                            table
                        )

                    else:

                        # Human timeout = check/call

                        if (
                            player["bet"]
                            ==
                            game["current_bet"]
                        ):

                            move_after_action(
                                table,
                                player
                            )

                        else:

                            action_call(
                                table,
                                player
                            )

                            move_after_action(
                                table,
                                player
                            )

                    continue

                # Bot thinks

                if player["is_bot"]:

                    elapsed = (
                        time.time()
                        -
                        game[
                            "turn_started_at"
                        ]
                        if game[
                            "turn_started_at"
                        ]
                        else 0
                    )

                    if elapsed >= random.uniform(
                        1.5,
                        4.0
                    ):

                        bot_action(
                            table
                        )

        except Exception as e:

            print(
                "BOT LOOP ERROR:",
                e
            )

        await asyncio.sleep(
            0.5
        )


@app.on_event("startup")
async def startup():

    asyncio.create_task(
        bot_loop()
    )


# =========================================================
# ADD BOTS
# =========================================================

@app.post("/add-bots")
def add_bots(
    table_id: int = 1
):

    table = get_table(
        table_id
    )

    if not table:

        return {
            "success": False,
            "message": "Table not found"
        }

    ensure_three_bots(
        table
    )

    return game_response(
        table
    )


# =========================================================
# RESET
# =========================================================

@app.post("/reset")
def reset(
    table_id: int = 1
):

    if table_id not in TABLES:

        return {
            "success": False,
            "message": "Table not found"
        }

    TABLES[table_id] = new_table(
        table_id
    )

    return {
        "success": True,
        "message":
            f"Table {table_id} reset"
    }


# =========================================================
# CHAT
# =========================================================

@app.post("/chat")
def chat(req: ChatRequest):

    table = get_table(
        req.table_id
    )

    if not table:

        return {
            "success": False
        }

    message = {
        "user_id":
            req.user_id,

        "message":
            req.message,

        "time":
            time.time()
    }

    table["chat"].append(
        message
    )

    table["chat"] = \
        table["chat"][-50:]

    return {
        "success": True,
        "messages":
            table["chat"]
    }


@app.get("/chat")
def get_chat(
    table_id: int = 1
):

    table = get_table(
        table_id
    )

    if not table:

        return {
            "success": False,
            "messages": []
        }

    return {
        "success": True,
        "messages":
            table["chat"]
    }
