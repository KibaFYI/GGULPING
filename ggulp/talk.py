"""Everything GGULPING says, in one place. Tweak the jokes here."""

TITLE = "g g u l p i n g"
EXIT_HINT = "type cmd-exit at any prompt to leave the table"

# setup screen
ASK_TOKEN = "First, the key to the pantry"
ASK_MODEL = "Which tasty model are you giving me?"
SAVORING = "mm… savoring it to know what to do with it"
ASK_NAME = "What should I call it once it's digested?"
ASK_PLACE = "Which shelf does it go on?"
ASK_MODE = "How hungry are we?"
ASK_STOMACH = "How big is my stomach?"
ASK_MENU = "What's on the menu?"
ASK_PICK = "À la carte"
ASK_WEBHOOK = "Discord pings (optional)"
READY = "Ready to dig in"

MODE_SMART = "Smart storage"
MODE_SMART_HINT = "small bites: when the next quant won't fit, I burp the finished ones up to the Hub to make room"
MODE_DUMP = "Dump"
MODE_DUMP_HINT = "every quant onto the disk, then one big upload. A full disk can break files"

MENU_BUFFET = "The full buffet"
MENU_SNACK = "The snack plate"
MENU_CUSTOM = "À la carte"

# the run
UNHINGE = "unhinging my jaw… turning it into one big BF16 GGUF"
FETCHING = "grabbing it off the Hub"
PORTIONS = "reading the menu (exact portion sizes)"
TASTING = "tasting it first to learn the flavor (importance matrix)"
SWALLOWED_SOURCE = "swallowed the download - only the GGUF is left"


def chewing(q: str) -> str:
    return f"chewing {q}…"


def swallowed(q: str, size: str) -> str:
    return f"swallowed {q} · {size}"


def burping(n: int, size: str) -> str:
    return f"burping {n} file{'s' if n != 1 else ''} ({size}) up to the Hub"


def stomach_full(need: str, room: str) -> str:
    return f"stomach's full - the next bite needs {need}, I only have {room}"


def side_of_plate(q: str, why: str) -> str:
    return f"pushed {q} to the side of the plate: {why}"


BURP_DONE = "burp. room made"
ALL_GULPED = "all gulped"
README_COPIED = "README copied over, untouched"
NO_README = "the model came without a README, so there was nothing to copy"
SICK = "ugh, that didn't go down well"
BYE = "full and happy"
