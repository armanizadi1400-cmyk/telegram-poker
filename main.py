from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import random
import asyncio
import itertools
from typing import Optional

app = FastAPI(title="Telegram Poker Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RANKS = "23456789TJQKA"
SUITS = ["♠", "♥", "♦", "♣"]

TABLE_COUNT = 6
MAX_SEATS = 6
BOTS_PER_TABLE = 3

STARTING_CHIPS = 1000
SMALL_BLIND = 10
BIG_BLIND = 20

TURN_SECONDS = 20


class JoinRequest(BaseModel):
    user_id: str
    name: str
    table_id: int


class ActionRequest(BaseModel):
    user_id: str
    action: str
    amount: int = 0
    table_id: int


class Player:
    def __init__(self, user_id, name, chips=STARTING_CHIPS, bot=False):
        self.user_id = str(user_id)
        self.name = name
        self.chips = chips
        self.cards = []
        self.bet = 0
        self.total_bet = 0
        self.folded = False
        self.all_in = False
        self.bot = bot

    def reset_for_hand(self):
        self.cards = []
        self.bet = 0
        self.total_bet = 0
        self.folded = False
        self.all_in = False


class PokerTable:
    def __init__(self, table_id):
        self.table_id = table_id
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

        self.acted_this_street = set()

        self.hand_number = 0
        self.winner = None
        self.message = ""

        self.turn_started_at = None

        self.lock = asyncio.Lock()
        self.bot_task = None


tables = [PokerTable(i + 1) for i in range(TABLE_COUNT)]


BOT_NAMES = [
    "Daniel", "Alex", "Michael", "David",
    "James", "Ryan", "Kevin", "Jack",
    "Thomas", "Chris", "Robert", "Adam"
]


def make_deck():
    return [r + s for r in RANKS for s in SUITS]


def card_value(card):
    return RANKS.index(card[0]) + 2


def is_red(card):
    return card[1] in ["♥", "♦"]


def hand_rank(cards):
    if len(cards) < 5:
        return (0,)

    values = sorted([card_value(c) for c in cards], reverse=True)

    best = (0,)

    for combo in itertools.combinations(cards, 5):
        vals = sorted([card_value(c) for c in combo], reverse=True)

        counts = {}
        for v in vals:
            counts[v] = counts.get(v, 0) + 1

        unique = sorted(set(vals), reverse=True)

        if 14 in unique:
            unique.append(1)

        straight_high = None

        for i in range(len(unique) - 4):
            part = unique[i:i + 5]
            if part[0] - part[4] == 4:
                straight_high = part[0]
                break

        flush = len(set(c[1] for c in combo)) == 1

        if flush and straight_high:
            score = (8, straight_high)

        else:
            groups = sorted(
                [(count, value) for value, count in counts.items()],
                reverse=True
            )

            if groups[0][0] == 4:
                four = groups[0][1]
                kicker = max(v for v in vals if v != four)
                score = (7, four, kicker)

            elif groups[0][0] == 3 and groups[1][0] >= 2:
                trips = groups[0][1]
                pair = groups[1][1]
                score = (6, trips, pair)

            elif flush:
                score = (5, *vals)

            elif straight_high:
                score = (4, straight_high)

            elif groups[0][0] == 3:
                trips = groups[0][1]
                kickers = sorted(
                    [v for v in vals if v != trips],
                    reverse=True
                )
                score = (3, trips, *kickers[:2])

            elif groups[0][0] == 2 and groups[1][0] == 2:
                pairs = sorted(
                    [groups[0][1], groups[1][1]],
                    reverse=True
                )
                kicker = max(
                    v for v in vals
                    if v not in pairs
                )
                score = (2, *pairs, kicker)

            elif groups[0][0] == 2:
                pair = groups[0][1]
                kickers = sorted(
                    [v for v in vals if v != pair],
                    reverse=True
                )
                score = (1, pair, *kickers[:3])

            else:
                score = (0, *vals)

        if score > best:
            best = score

    return best


def active_players(table):
    return [
        p for p in table.players
        if not p.folded
    ]


def players_who_can_act(table):
    return [
        p for p in table.players
        if not p.folded and not p.all_in and p.chips > 0
    ]


def betting_round_complete(table):
    active = players_who_can_act(table)

    if not active:
        return True

    for p in active:
        if p.user_id not in table.acted_this_street:
            return False

        if p.bet != table.current_bet:
            return False

    return True


def next_active_index(table, start):
    count = len(table.players)

    for step in range(1, count + 1):
        idx = (start + step) % count
        p = table.players[idx]

        if not p.folded and not p.all_in and p.chips > 0:
            return idx

    return None


def all_in_or_one_left(table):
    active = [
        p for p in table.players
        if not p.folded
    ]

    if len(active) <= 1:
        return True

    return all(p.all_in for p in active)


def reset_street(table):
    for p in table.players:
        p.bet = 0

    table.current_bet = 0
    table.min_raise = BIG_BLIND
    table.acted_this_street = set()


def collect_pot(table):
    table.pot = sum(p.total_bet for p in table.players)


def deal_hole_cards(table):
    for _ in range(2):
        for p in table.players:
            if not p.folded:
                p.cards.append(table.deck.pop())


def post_blinds(table):
    n = len(table.players)

    if n < 2:
        return

    sb_index = (table.dealer_index + 1) % n
    bb_index = (table.dealer_index + 2) % n

    sb = table.players[sb_index]
    bb = table.players[bb_index]

    sb_amount = min(SMALL_BLIND, sb.chips)
    bb_amount = min(BIG_BLIND, bb.chips)

    sb.chips -= sb_amount
    sb.bet = sb_amount
    sb.total_bet = sb_amount
    if sb.chips == 0:
        sb.all_in = True

    bb.chips -= bb_amount
    bb.bet = bb_amount
    bb.total_bet = bb_amount
    if bb.chips == 0:
        bb.all_in = True

    table.current_bet = bb_amount
    table.min_raise = BIG_BLIND

    table.current_index = next_active_index(table, bb_index)

    if table.current_index is None:
        table.current_index = 0


async def start_hand(table):
    if len(table.players) < 2:
        return

    table.hand_number += 1
    table.started = True
    table.stage = "preflop"
    table.winner = None
    table.message = ""
    table.community = []
    table.pot = 0
    table.deck = make_deck()
    random.shuffle(table.deck)

    for p in table.players:
        p.reset_for_hand()

    deal_hole_cards(table)
    post_blinds(table)

    collect_pot(table)

    table.acted_this_street = set()

    # Big blind has not acted yet preflop.
    table.turn_started_at = asyncio.get_event_loop().time()


async def advance_street(table):
    if table.stage == "preflop":
        table.stage = "flop"

        # Burn one
        table.deck.pop()

        # Flop = EXACTLY 3 cards
        table.community.extend([
            table.deck.pop(),
            table.deck.pop(),
            table.deck.pop()
        ])

    elif table.stage == "flop":
        table.stage = "turn"

        table.deck.pop()
        table.community.append(table.deck.pop())

    elif table.stage == "turn":
        table.stage = "river"

        table.deck.pop()
        table.community.append(table.deck.pop())

    elif table.stage == "river":
        await finish_hand(table)
        return

    reset_street(table)

    first = next_active_index(table, table.dealer_index)

    if first is None:
        await finish_hand(table)
        return

    table.current_index = first
    table.turn_started_at = asyncio.get_event_loop().time()

    collect_pot(table)


async def finish_hand(table):
    active = [
        p for p in table.players
        if not p.folded
    ]

    if not active:
        return

    if len(active) == 1:
        winner = active[0]
        winner.chips += table.pot
        table.winner = winner.name
        table.message = f"{winner.name} wins {table.pot} chips"
    else:
        ranked = []

        for p in active:
            score = hand_rank(p.cards + table.community)
            ranked.append((score, p))

        ranked.sort(
            key=lambda x: x[0],
            reverse=True
        )

        best_score = ranked[0][0]
        winners = [
            p for score, p in ranked
            if score == best_score
        ]

        share = table.pot // len(winners)

        for p in winners:
            p.chips += share

        remainder = table.pot - share * len(winners)

        if remainder:
            winners[0].chips += remainder

        if len(winners) == 1:
            table.winner = winners[0].name
            table.message = f"{winners[0].name} wins {table.pot} chips"
        else:
            table.winner = "Split pot"
            table.message = "Split pot"

    table.pot = 0
    table.started = False
    table.stage = "finished"
    table.current_index = -1
    table.turn_started_at = None

    # Waiting players join after the hand.
    for p in table.waiting:
        if len(table.players) < MAX_SEATS:
            table.players.append(p)

    table.waiting.clear()

    await asyncio.sleep(3)

    table.stage = "waiting"

    if len(table.players) >= 2:
        await start_hand(table)


def can_join_active(table):
    return len(table.players) < MAX_SEATS


def add_bots(table):
    existing = {
        p.user_id
        for p in table.players
    }

    while len([
        p for p in table.players
        if p.bot
    ]) < BOTS_PER_TABLE and len(table.players) < MAX_SEATS:

        name = random.choice(BOT_NAMES)

        bot_id = f"bot_{table.table_id}_{random.randint(1000, 9999)}"

        while bot_id in existing:
            bot_id = f"bot_{table.table_id}_{random.randint(1000, 9999)}"

        existing.add(bot_id)

        bot = Player(
            bot_id,
            name,
            random.randint(700, 1500),
            True
        )

        table.players.append(bot)


async def bot_action(table):
    if table.current_index < 0:
        return

    if table.current_index >= len(table.players):
        return

    p = table.players[table.current_index]

    if not p.bot:
        return

    await asyncio.sleep(random.uniform(1.0, 2.2))

    if p.folded or p.all_in:
        return

    to_call = max(0, table.current_bet - p.bet)

    strength = random.random()

    if to_call == 0:
        if strength < 0.15:
            await perform_action(table, p, "raise", BIG_BLIND * 2)
        else:
            await perform_action(table, p, "check", 0)

    else:
        if strength < 0.10 and to_call > BIG_BLIND:
            await perform_action(table, p, "fold", 0)

        elif strength < 0.30:
            await perform_action(table, p, "call", 0)

        elif strength < 0.55:
            await perform_action(
                table,
                p,
                "raise",
                max(BIG_BLIND * 2, table.current_bet * 2)
            )

        else:
            await perform_action(table, p, "call", 0)


async def perform_action(table, player, action, amount=0):
    if player.folded or player.all_in:
        return

    if action == "fold":
        player.folded = True
        table.acted_this_street.add(player.user_id)

    elif action == "check":
        if player.bet != table.current_bet:
            return

        table.acted_this_street.add(player.user_id)

    elif action == "call":
        to_call = max(
            0,
            table.current_bet - player.bet
        )

        pay = min(to_call, player.chips)

        player.chips -= pay
        player.bet += pay
        player.total_bet += pay

        if player.chips == 0:
            player.all_in = True

        table.acted_this_street.add(player.user_id)

    elif action == "raise":
        target = max(
            table.current_bet + table.min_raise,
            amount
        )

        needed = target - player.bet

        if needed <= 0:
            return

        needed = min(needed, player.chips)

        player.chips -= needed
        player.bet += needed
        player.total_bet += needed

        if player.bet > table.current_bet:
            table.min_raise = max(
                BIG_BLIND,
                player.bet - table.current_bet
            )
            table.current_bet = player.bet

            table.acted_this_street = {
                player.user_id
            }
        else:
            table.acted_this_street.add(
                player.user_id
            )

        if player.chips == 0:
            player.all_in = True

    elif action == "allin":
        amount_to_put = player.chips

        player.chips = 0
        player.bet += amount_to_put
        player.total_bet += amount_to_put
        player.all_in = True

        if player.bet > table.current_bet:
            table.min_raise = max(
                BIG_BLIND,
                player.bet - table.current_bet
            )
            table.current_bet = player.bet

            table.acted_this_street = {
                player.user_id
            }
        else:
            table.acted_this_street.add(
                player.user_id
            )

    collect_pot(table)

    remaining = [
        p for p in table.players
        if not p.folded
    ]

    if len(remaining) == 1:
        await finish_hand(table)
        return

    if all_in_or_one_left(table):
        # If everyone is all-in, reveal remaining board
        # in correct order.
        while table.stage != "finished" and len(table.community) < 5:
            if table.stage == "preflop":
                table.stage = "flop"
                table.deck.pop()
                table.community.extend([
                    table.deck.pop(),
                    table.deck.pop(),
                    table.deck.pop()
                ])

            elif table.stage == "flop":
                table.stage = "turn"
                table.deck.pop()
                table.community.append(table.deck.pop())

            elif table.stage == "turn":
                table.stage = "river"
                table.deck.pop()
                table.community.append(table.deck.pop())

            elif table.stage == "river":
                await finish_hand(table)
                return

        await finish_hand(table)
        return

    if betting_round_complete(table):
        await advance_street(table)
        return

    nxt = next_active_index(
        table,
        table.current_index
    )

    if nxt is not None:
        table.current_index = nxt
        table.turn_started_at = asyncio.get_event_loop().time()


async def table_loop(table):
    while True:
        try:
            if (
                not table.started
                and table.stage in ["waiting", "finished"]
                and len(table.players) >= 2
            ):
                await start_hand(table)

            if table.started and table.current_index >= 0:
                if table.current_index < len(table.players):
                    current = table.players[
                        table.current_index
                    ]

                    if current.bot:
                        await bot_action(table)

                    else:
                        if table.turn_started_at:
                            elapsed = (
                                asyncio.get_event_loop().time()
                                - table.turn_started_at
                            )

                            if elapsed >= TURN_SECONDS:
                                await perform_action(
                                    table,
                                    current,
                                    "fold",
                                    0
                                )

            await asyncio.sleep(0.15)

        except Exception as e:
            print(
                f"TABLE {table.table_id} ERROR:",
                e
            )
            await asyncio.sleep(1)


@app.on_event("startup")
async def startup():
    for table in tables:
        table.bot_task = asyncio.create_task(
            table_loop(table)
        )


@app.get("/")
async def root():
    return {
        "status": "online",
        "message": "Poker backend is running"
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/tables")
async def get_tables():
    result = []

    for table in tables:
        result.append({
            "table_id": table.table_id,
            "players": len(table.players),
            "max_players": MAX_SEATS,
            "stage": table.stage,
            "started": table.started,
            "pot": table.pot
        })

    return {
        "success": True,
        "tables": result
    }


@app.post("/join")
async def join(req: JoinRequest):
    if req.table_id < 1 or req.table_id > TABLE_COUNT:
        raise HTTPException(
            status_code=400,
            detail="Invalid table"
        )

    table = tables[req.table_id - 1]

    # Remove user from every table first.
    for t in tables:
        t.players = [
            p for p in t.players
            if p.user_id != str(req.user_id)
        ]

        t.waiting = [
            p for p in t.waiting
            if p.user_id != str(req.user_id)
        ]

    existing = next(
        (
            p for p in table.players
            if p.user_id == str(req.user_id)
        ),
        None
    )

    if existing:
        return {
            "success": True,
            "status": "already_joined",
            "table_id": table.table_id
        }

    player = Player(
        req.user_id,
        req.name or "Player"
    )

    # Active hand: wait for next hand.
    if table.started:
        if len(table.players) + len(table.waiting) >= MAX_SEATS:
            raise HTTPException(
                status_code=400,
                detail="Table is full"
            )

        table.waiting.append(player)

        return {
            "success": True,
            "status": "waiting",
            "table_id": table.table_id,
            "message": "Waiting for next hand"
        }

    if len(table.players) >= MAX_SEATS:
        raise HTTPException(
            status_code=400,
            detail="Table is full"
        )

    table.players.append(player)

    # Exactly 3 bots when table starts.
    add_bots(table)

    return {
        "success": True,
        "status": "joined",
        "table_id": table.table_id
    }


@app.post("/leave")
async def leave(req: JoinRequest):
    for table in tables:
        table.players = [
            p for p in table.players
            if p.user_id != str(req.user_id)
        ]

        table.waiting = [
            p for p in table.waiting
            if p.user_id != str(req.user_id)
        ]

    return {
        "success": True
    }


@app.get("/game")
async def game(
    table_id: int,
    user_id: Optional[str] = None
):
    if table_id < 1 or table_id > TABLE_COUNT:
        raise HTTPException(
            status_code=400,
            detail="Invalid table"
        )

    table = tables[table_id - 1]

    current = None

    if (
        table.current_index >= 0
        and table.current_index < len(table.players)
    ):
        current = table.players[
            table.current_index
        ]

    remaining_seconds = TURN_SECONDS

    if table.turn_started_at:
        elapsed = (
            asyncio.get_event_loop().time()
            - table.turn_started_at
        )

        remaining_seconds = max(
            0,
            TURN_SECONDS - int(elapsed)
        )

    players_data = []

    for i, p in enumerate(table.players):
        players_data.append({
            "user_id": p.user_id,
            "name": p.name,
            "chips": p.chips,
            "bet": p.bet,
            "folded": p.folded,
            "all_in": p.all_in,
            "is_turn": i == table.current_index,
            "bot": p.bot
        })

    my_cards = []

    if user_id:
        me = next(
            (
                p for p in table.players
                if p.user_id == str(user_id)
            ),
            None
        )

        if me:
            my_cards = me.cards

    return {
        "success": True,
        "table_id": table.table_id,
        "stage": table.stage,
        "started": table.started,
        "community_cards": table.community,
        "pot": table.pot,
        "current_player": (
            current.user_id if current else None
        ),
        "current_player_name": (
            current.name if current else None
        ),
        "current_bet": table.current_bet,
        "winner": table.winner,
        "message": table.message,
        "players": players_data,
        "my_cards": my_cards,
        "timer": remaining_seconds,
        "waiting": [
            {
                "user_id": p.user_id,
                "name": p.name
            }
            for p in table.waiting
        ]
    }


@app.get("/my-cards")
async def my_cards(
    table_id: int,
    user_id: str
):
    table = tables[table_id - 1]

    player = next(
        (
            p for p in table.players
            if p.user_id == str(user_id)
        ),
        None
    )

    if not player:
        return {
            "success": False,
            "cards": []
        }

    return {
        "success": True,
        "cards": player.cards
    }


@app.post("/action")
async def action(req: ActionRequest):
    if req.table_id < 1 or req.table_id > TABLE_COUNT:
        raise HTTPException(
            status_code=400,
            detail="Invalid table"
        )

    table = tables[req.table_id - 1]

    if not table.started:
        return {
            "success": False,
            "message": "Game is not active"
        }

    if (
        table.current_index < 0
        or table.current_index >= len(table.players)
    ):
        return {
            "success": False,
            "message": "No active player"
        }

    player = table.players[
        table.current_index
    ]

    if player.user_id != str(req.user_id):
        return {
            "success": False,
            "message": "Not your turn"
        }

    await perform_action(
        table,
        player,
        req.action.lower(),
        req.amount
    )

    return {
        "success": True
    }


@app.post("/reset")
async def reset():
    global tables

    tables = [
        PokerTable(i + 1)
        for i in range(TABLE_COUNT)
    ]

    for table in tables:
        table.bot_task = asyncio.create_task(
            table_loop(table)
        )

    return {
        "success": True,
        "message": "All tables reset"
    }
