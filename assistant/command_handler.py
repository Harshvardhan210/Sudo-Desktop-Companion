import time

from features.app_launcher import open_application

from features.reminder import reminders

from features.app_closer import parse_close_command, close_application

from features.web_tools import handle_web_command

from features.notes import notes, Reply

from features.todo import todos

from features.routines import routines

from assistant.voice_input import strip_wake_word

from assistant import audio_devices, hindi_commands, wake_mode
from assistant.brain import brain

from assistant.voice_output import (
    next_voice,
    set_voice_gender,
    change_speed
)

from features.file_search import (
    search_files,
    open_folder,
    open_search_result
)

from features.system_control import (
    get_time,
    get_date,
    get_battery_status,
    change_volume,
    mute_volume,
    unmute_volume,
    lock_computer,
    shutdown_computer,
    restart_computer,
    cancel_shutdown
)


# Stores pending shutdown/restart confirmation
pending_action = None

# Pause between the steps of a routine (seconds), so apps have time to start
ROUTINE_STEP_DELAY = 0.8


def _show_only(owner):
    """Only one card (notes, to-do or routines) is shown on the left at a time."""

    cards = {"notes": notes, "todo": todos, "routines": routines}

    if not cards[owner].card_open:
        return

    for name, manager in cards.items():
        if name != owner and manager.card_open:
            manager.close_card()


def _run_routine(reply):
    """Do every step of a routine, one after the other."""

    lines = []

    for number, step in enumerate(reply.run_steps, 1):
        try:
            # "sudo" makes each step a normal command, even during a note.
            result = str(process_command("sudo " + step)).strip().splitlines()
            result = result[0] if result else "done"
        except Exception as error:
            result = f"error: {error}"

        lines.append(f"{number}. {step}: {result}")
        time.sleep(ROUTINE_STEP_DELAY)

    return Reply(
        f"Ran the {reply.run_name} routine:\n" + "\n".join(lines),
        speech=reply.speech
    )


def _brain_run(command):
    """Run a command the brain worked out (it may not ask the brain again)."""

    return _process(command, {}, allow_brain=False)


def process_command(command):
    """Understand one command and do it. Also counts how often you use each action."""

    context = {}
    result = _process(command, context, allow_brain=True)

    action_text = context.get("action_text")

    if (action_text and not getattr(result, "brain_ask", False)
            and not str(result).startswith("Sorry, I don't understand")):
        brain.record_usage(action_text)

    return result


def _process(command, context, allow_brain):
    global pending_action

    original = command.strip()
    command = original.lower()

    # "sudo ..." can arrive with the wake word still attached (while taking
    # notes, or when typed). Keep both versions.
    heard_wake, without_wake = strip_wake_word(original)

    if heard_wake and not without_wake:
        return "Yes? Say your command."

    # -----------------------------
    # REMINDERS
    # -----------------------------

    # The assistant asked for a reminder name: this reply is the name.
    if reminders.has_pending():
        return reminders.handle_name(without_wake.lower() if heard_wake else command)

    # The assistant asked "What's the task?": this reply is the task.
    if todos.has_pending():
        return todos.handle_pending(without_wake if heard_wake else original)

    # The assistant asked what you meant / what to answer: this reply is the answer.
    if allow_brain and brain.has_pending():
        return brain.handle_pending(without_wake if heard_wake else original, _brain_run)

    # -----------------------------
    # HINDI / HINGLISH: turn it into the English command first
    # -----------------------------

    # (While taking notes or creating a routine, what you say without "sudo"
    # is note text / steps, so it is not translated.)
    in_session = notes.active or routines.busy()

    if wake_mode.hindi_enabled() and not (in_session and not heard_wake):
        translated = hindi_commands.to_english(without_wake if heard_wake else original)

        if translated:
            original = translated
            without_wake = translated
            command = translated.lower()

    # -----------------------------
    # BRAIN: call X Y / forget ... / my habits / nicknames
    # -----------------------------

    if not (in_session and not heard_wake):
        brain_reply = brain.handle_management(without_wake if heard_wake else original)

        if brain_reply is not None:
            return brain_reply

        # Nicknames: "open browser" -> "open chrome"
        expanded = brain.expand_aliases(without_wake if heard_wake else original)

        if expanded != (without_wake if heard_wake else original):
            original = expanded
            without_wake = expanded
            command = expanded.lower()

        context["action_text"] = (without_wake if heard_wake else original).lower()

    # -----------------------------
    # ROUTINES (create routine / run work routine / close work routine)
    # -----------------------------

    routine_response = routines.handle(
        original,
        heard_wake,
        without_wake,
        busy_with="taking notes" if notes.active else None
    )

    if routine_response is not None:
        _show_only("routines")

        if getattr(routine_response, "run_steps", None):
            return _run_routine(routine_response)

        return routine_response

    # -----------------------------
    # NOTES (add a note / open notes / save notes)
    # -----------------------------

    notes_response = notes.handle(original, heard_wake, without_wake)

    if notes_response is not None:
        _show_only("notes")
        return notes_response

    # -----------------------------
    # TO-DO LIST (add task / show tasks / mark task done ...)
    # -----------------------------

    todo_response = todos.handle(original, heard_wake, without_wake)

    if todo_response is not None:
        _show_only("todo")
        return todo_response

    # From here on the wake word is not needed.
    if heard_wake:
        command = without_wake.lower().strip()

    if reminders.is_reminder_request(command):
        return reminders.start_request(command)

    if reminders.is_cancel_request(command):
        return reminders.cancel_all()

    # -----------------------------
    # CLOSE APPLICATIONS
    # -----------------------------

    close_request = parse_close_command(command)

    if close_request:
        target, force = close_request
        return close_application(target, force)

    # -----------------------------
    # WEB: search, open website, play YouTube video
    # -----------------------------

    web_response = handle_web_command(command)

    if web_response is not None:
        return web_response

    # -----------------------------
    # WAKE WORD ("sudo" optional or required)
    # -----------------------------

    if command in ["wake word off", "always listen", "listen without sudo",
                   "no wake word", "stop requiring sudo", "hands free mode",
                   "hands-free mode", "sudo is optional"]:
        return Reply(wake_mode.set_wake_required(False))

    if command in ["wake word on", "require wake word", "use wake word",
                   "only listen for sudo", "sudo required", "always need sudo"]:
        return Reply(wake_mode.set_wake_required(True))

    # -----------------------------
    # EARPHONES / MICROPHONE
    # -----------------------------

    if command in ["use headphones", "use earphones", "use my earphones",
                   "use my headphones", "use headset", "use my headset",
                   "use bluetooth headset", "use earbuds", "use my earbuds"]:
        return audio_devices.set_mode("headset")

    if command in ["use laptop mic", "use laptop microphone", "use built in mic",
                   "use built in microphone", "use the laptop mic", "use speakers",
                   "use laptop speakers"]:
        return audio_devices.set_mode("laptop")

    if command in ["use automatic audio", "automatic audio", "auto audio",
                   "audio auto", "use auto audio", "switch audio automatically"]:
        return audio_devices.set_mode("auto")

    if command in ["which mic are you using", "which microphone are you using",
                   "what mic are you using", "what microphone are you using",
                   "audio status", "which headphones are you using",
                   "are you using my earphones", "which mic"]:
        return Reply(audio_devices.status_text())

    # -----------------------------
    # ROBOT VOICE
    # -----------------------------

    if command in ["change voice", "next voice", "switch voice"]:
        return next_voice()

    if command in ["male voice", "use male voice", "switch to male voice",
                   "change to male voice", "use a male voice"]:
        return set_voice_gender("male")

    if command in ["female voice", "use female voice", "switch to female voice",
                   "change to female voice", "use a female voice"]:
        return set_voice_gender("female")

    if command in ["speak faster", "talk faster", "speed up"]:
        return change_speed(10)

    if command in ["speak slower", "talk slower", "slow down"]:
        return change_speed(-10)

    # -----------------------------
    # BASIC COMMANDS
    # -----------------------------

    if command in ["hello", "hi", "hey"]:
        return "Hello! How can I help you?"

    elif command in ["help", "what can you do"]:
        return """
PERSONAL DESKTOP ASSISTANT

APPLICATIONS:
- Open Chrome
- Open VS Code
- Open Notepad
- Open Calculator
- Close Notepad
- Force close Chrome

FILE SEARCH:
- Find my resume
- Search Python projects
- Where is my resume
- Open result 1

FOLDERS:
- Open Desktop
- Open Documents
- Open Downloads
- Open Pictures

ROUTINES:
- Create routine (then say its name, then: add open chrome, open youtube)
- Close <name> routine (saves it while creating)
- Run <name> routine
- Close <name> routine (closes its apps, after it was saved)
- Show routines / Delete <name> routine

TO-DO LIST:
- Add task buy milk
- Show my tasks
- Mark task 2 as done
- Delete task 3
- Clear completed tasks

NOTES:
- Add a note (then say each point)
- Open notes
- Save notes
- Delete last point
- Cancel notes

BRAIN (it learns from you):
- Call chrome as browser (a nickname)
- Forget <question> / Forget alias browser
- My habits / What have you learned / Show aliases
- Ask something I don't know: I'll ask for the answer and remember it

HINDI AND ENGLISH (with or without sudo):
- Volume ko kam karo / Awaaz badhao
- Notepad kholo / Chrome band karo
- 5 minute baad yaad dilao
- Work routine shuru karo
- Wake word off (sudo not needed) / Wake word on

EARPHONES / MICROPHONE:
- Use headphones / Use earphones
- Use laptop mic
- Use automatic audio
- Which mic are you using

ROBOT VOICE:
- Change voice
- Male voice / Female voice
- Speak faster / Speak slower

WEB:
- Google python tutorials
- Search the web for best laptops
- Search YouTube for lofi music
- Play shape of you
- Open YouTube
- Open github.com
- Go to flipkart

REMINDERS:
- Remind me in 5 minutes
- Remind me to drink water in 10 minutes
- Cancel reminders

SYSTEM CONTROL:
- What time is it
- What is today's date
- Battery status
- Volume up
- Volume down
- Mute volume
- Unmute volume
- Lock computer
- Shutdown
- Restart
- Cancel shutdown

EXIT:
- Exit
- Quit
- Bye
"""

    # -----------------------------
    # CONFIRMATION COMMANDS
    # -----------------------------

    elif command == "confirm shutdown":
        if pending_action == "shutdown":
            pending_action = None
            return shutdown_computer()

        return "There is no pending shutdown request."

    elif command == "confirm restart":
        if pending_action == "restart":
            pending_action = None
            return restart_computer()

        return "There is no pending restart request."

    elif command in ["cancel", "cancel shutdown"]:
        pending_action = None
        return cancel_shutdown()

    # -----------------------------
    # SYSTEM INFORMATION
    # -----------------------------

    elif command in ["time", "what time is it"]:
        return get_time()

    elif command in ["date", "what is today's date", "today's date"]:
        return get_date()

    elif command in ["battery", "battery status", "check battery"]:
        return get_battery_status()

    # -----------------------------
    # VOLUME CONTROL
    # -----------------------------

    elif command in ["volume up", "increase volume"]:
        return change_volume(0.10)

    elif command in ["volume down", "decrease volume"]:
        return change_volume(-0.10)

    elif command in ["mute", "mute volume"]:
        return mute_volume()

    elif command in ["unmute", "unmute volume"]:
        return unmute_volume()

    # -----------------------------
    # WINDOWS CONTROL
    # -----------------------------

    elif command in ["lock", "lock computer", "lock pc"]:
        return lock_computer()

    elif command in ["shutdown", "shut down", "shutdown computer"]:
        pending_action = "shutdown"
        return "Are you sure you want to shut down? Type confirm shutdown to proceed."

    elif command in ["restart", "restart computer"]:
        pending_action = "restart"
        return "Are you sure you want to restart? Type confirm restart to proceed."

    # -----------------------------
    # FILE SEARCH
    # -----------------------------

    elif command.startswith("open result "):
        try:
            index = int(command.replace("open result ", "", 1))
            return open_search_result(index)

        except ValueError:
            return "Please provide a valid result number."

    elif command.startswith("find "):
        return search_files(command[5:].strip())

    elif command.startswith("search "):
        return search_files(command[7:].strip())

    elif command.startswith("where is "):
        return search_files(command[9:].strip())

    # -----------------------------
    # OPEN APPLICATIONS / FOLDERS
    # -----------------------------

    elif command.startswith("open "):
        target = command[5:].strip()

        common_folders = [
            "desktop",
            "documents",
            "downloads",
            "pictures",
            "music",
            "videos"
        ]

        if target in common_folders:
            return open_folder(target)

        return open_application(target)

    elif command.startswith("launch "):
        return open_application(command[7:].strip())

    elif command.startswith("start "):
        return open_application(command[6:].strip())

    # -----------------------------
    # EXIT
    # -----------------------------

    elif command in ["exit", "quit", "bye"]:
        pending_action = None
        return "Goodbye!"

    else:
        if allow_brain:
            # Not a command I know: understand it from what I have learned,
            # or ask you (and learn the answer).
            return brain.handle_unknown(without_wake if heard_wake else original, _brain_run)

        return "Sorry, I don't understand that command."