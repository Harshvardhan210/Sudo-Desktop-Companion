"""
Do I need to say "sudo" first?

  wake word OFF (default): commands that clearly look like commands work without
                "sudo", like "open notepad" or "start work routine". Everything
                else that is said is ignored, so normal conversation does not
                trigger the robot.
  wake word ON:  only phrases that start with "sudo" are used.

Some commands always need "sudo" because they are risky: shutdown, restart,
delete, remove, clear, kill, force, confirm.

Say "wake word on" / "wake word off" (or "always listen") to switch.
Hindi and English commands both work, with or without "sudo".
"""

import json
import re
from pathlib import Path

from assistant import hindi_commands
from assistant.brain import brain


SETTINGS_FILE = Path(__file__).with_name("wake_settings.json")

DEFAULT_SETTINGS = {
    "wake_required": False,    # False = "sudo" is optional
    "hindi": True,             # also listen for Hindi speech
}

# Without "sudo", longer sentences are ignored (they are probably a conversation)
MAX_WORDS_WITHOUT_WAKE = 9

# These always need "sudo"
_NEEDS_WAKE = re.compile(
    r"\b(shut\s?down|restart|reboot|delete|remove|clear|kill|terminate|force|confirm|erase|"
    r"call|forget)\b"
)
_NEEDS_WAKE_EXACT = {"hello", "hi", "hey", "help"}


def _load():
    settings = dict(DEFAULT_SETTINGS)

    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass

    return settings


settings = _load()


def _save():
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError as error:
        print("Could not save wake word settings:", error)


def wake_required():
    return bool(settings["wake_required"])


def hindi_enabled():
    return bool(settings["hindi"])


def set_wake_required(required):
    settings["wake_required"] = bool(required)
    _save()

    if required:
        return "Okay, I'll only listen to commands that start with sudo."

    return (
        "Okay, you don't need to say sudo now. "
        "Commands like shutdown, restart and delete still need it."
    )


# ----------------------------------------------------------------------------
# Does this sentence look like a command? (used when "sudo" was not said)
# ----------------------------------------------------------------------------

_THING = r"(?:notes?|tasks?|to ?do(?: list)?|routines?|reminders?)"

_ENGLISH_COMMANDS = [
    # open / close / start an app, site or folder (one or two words)
    re.compile(r"^(?:open|launch|start|close|quit)\s+(?:the\s+|my\s+)?(?!the\b)\S+(?:\s+\S+)?$"),
    # routines
    re.compile(r"^(?:run|start|open|launch|begin|close|show|create|make|new)\s+(?:the\s+|my\s+|a\s+)?(?:\S+\s+){0,3}routines?$"),
    # web and media
    re.compile(r"^(?:play|google|search|find|look up|lookup|visit|browse|go to)\s+\S+.*$"),
    # reminders, notes, tasks
    re.compile(r"^(?:remind me|set (?:a )?reminder|create (?:a )?reminder|add (?:a )?reminder)\b.*$"),
    re.compile(rf"^(?:add|create|new|make|take|write)\s+(?:a\s+|an\s+)?(?:new\s+)?{_THING}\b.*$"),
    re.compile(rf"^(?:show|open|view|display|read|close|hide)\s+(?:my\s+|the\s+|all\s+)*{_THING}$"),
    re.compile(r"^(?:save|finish)\s+(?:my\s+|the\s+)?notes?$"),
    re.compile(r"^(?:mark|complete|finish|tick|undo)\s+(?:task|todo|to do)\b.*$"),
    re.compile(r"^(?:mark)\s+.+\s+(?:as\s+)?(?:done|complete|completed)$"),
    # system
    re.compile(r"^(?:volume up|volume down|increase volume|decrease volume|mute|unmute|mute volume|unmute volume)$"),
    re.compile(r"^(?:what time is it|what is the time|what's the time|what is today's date|what's today's date|today's date|battery status|check battery|battery)$"),
    re.compile(r"^lock (?:computer|pc|laptop)$"),
    # the robot's own settings
    re.compile(
        r"^(?:change voice|male voice|female voice|speak faster|speak slower|use headphones|"
        r"use earphones|use laptop mic|use automatic audio|which mic are you using|"
        r"wake word on|wake word off|always listen|require wake word)$"
    ),
]


def looks_like_command(text):
    """
    True if the sentence is clearly a command (so it can run without "sudo").
    English and Hindi/Hinglish are both understood.
    """

    translated = hindi_commands.to_english(text) if hindi_enabled() else None

    candidate = translated or re.sub(r"[^\w\s']", " ", text.lower())
    candidate = " ".join(candidate.split())

    if not candidate:
        return False

    if len(candidate.split()) > MAX_WORDS_WITHOUT_WAKE:
        return False

    if _NEEDS_WAKE.search(candidate) or candidate in _NEEDS_WAKE_EXACT:
        return False

    if translated:
        return True

    if any(pattern.match(candidate) for pattern in _ENGLISH_COMMANDS):
        return True

    # Something the brain has learned (a taught question, or a way of saying an action)
    return brain.recognizes(candidate)