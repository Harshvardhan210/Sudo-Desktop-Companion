import json
import time

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

try:
    from PySide6.QtMultimedia import QMediaDevices   # tells us when earphones are (un)plugged
except ImportError:
    QMediaDevices = None

from assistant import audio_devices
from assistant.brain import brain
from assistant.voice_input import listen_continuously
from assistant.command_handler import process_command
from assistant.voice_output import speak
from features.reminder import reminders, PENDING_SECONDS
from features.notes import notes, NOTE_IDLE_SECONDS
from features.todo import todos, PENDING_SECONDS as TODO_PENDING_SECONDS
from features.routines import routines


# Speak the answer aloud for commands typed in the search bar?
# (False = show the reply as text only, so typing is never blocked by speech)
SPEAK_TEXT_REPLIES = False


class VoiceWorker(QThread):
    """Listens continuously in the background and reports "sudo ..." commands."""

    command_ready = Signal(str)
    listening_error = Signal(str)

    def __init__(self):
        super().__init__()
        self._paused = False
        self._ignore_until = 0.0
        self._answer_until = 0.0

    def expect_answer(self, seconds):
        """The assistant asked a question: next phrase needs no wake word."""
        self._answer_until = time.time() + seconds

    def _is_expecting_answer(self):
        return time.time() < self._answer_until

    def pause(self):
        """Stop picking up speech (while a command runs / the assistant talks)."""
        self._paused = True

    def resume(self, delay=0.8):
        """Start listening again (with a short delay so the echo is ignored)."""
        self._ignore_until = time.time() + delay
        self._paused = False

    def _should_stop(self):
        return self.isInterruptionRequested()

    def _is_paused(self):
        return self._paused or time.time() < self._ignore_until

    def run(self):
        try:
            # Headset microphone if earphones are connected, otherwise the laptop mic
            index, name, kind = audio_devices.pick_input()
            audio_devices.set_current_input(name, kind)
            print(f"Assistant: Microphone: {name} ({kind})")

            for command in listen_continuously(
                self._should_stop,
                self._is_paused,
                self._is_expecting_answer,
                index,
                lambda: audio_devices.set_current_input("default microphone", "default"),
            ):
                # Stay silent until the assistant has finished answering.
                self._paused = True
                self._answer_until = 0.0
                self.command_ready.emit(command)

        except OSError as error:
            print("Assistant: Microphone error.")
            print("Error:", error)
            self.listening_error.emit(f"Microphone error: {error}")

        except Exception as error:
            print("Assistant: Listening stopped unexpectedly.")
            print("Error:", error)
            self.listening_error.emit(f"Listening error: {error}")


class AssistantBridge(QObject):
    commandRecognized = Signal(str)
    assistantResponse = Signal(str)
    statusChanged = Signal(str)
    remindersChanged = Signal(str)   # JSON list of active reminders (for the countdown)
    reminderDue = Signal(str)        # a reminder's time is up (name)
    reminderDone = Signal(str)       # finished speaking the reminder name
    notesChanged = Signal(str)       # JSON: the notes card (draft + saved notes)
    todoChanged = Signal(str)        # JSON: the to-do card (tasks)
    routinesChanged = Signal(str)    # JSON: the routines card

    def __init__(self):
        super().__init__()
        self.voice_worker = None

        # Checks twice a second for finished reminders.
        self._last_reminder_version = 0
        self._restart_pending = False              # reopen the microphone when it stops
        self._audio_restart_after_speech = False   # audio mode changed by a command
        self._last_audio_version = audio_devices.version

        # Earphones plugged in / out: reopen the microphone with the right device.
        self._media_devices = None
        if QMediaDevices is not None:
            try:
                self._media_devices = QMediaDevices(self)
                self._media_devices.audioInputsChanged.connect(self._audio_devices_changed)
                self._media_devices.audioOutputsChanged.connect(self._audio_devices_changed)
            except Exception as error:
                print("Could not watch audio devices:", error)

        self._last_notes_version = 0
        self._last_todo_version = 0
        self._last_routines_version = 0
        self.reminder_timer = QTimer(self)
        self.reminder_timer.setInterval(500)
        self.reminder_timer.timeout.connect(self._reminder_tick)
        self.reminder_timer.start()

    # ------------------------------------------------------------------
    # Reminders
    # ------------------------------------------------------------------

    def _sync_reminders(self):
        """Send the active reminder list to the page when it changed."""
        if reminders.version != self._last_reminder_version:
            self._last_reminder_version = reminders.version
            self.remindersChanged.emit(json.dumps(reminders.snapshot()))

    @Slot(result=str)
    def getReminders(self):
        """The page asks for the current reminders (backup for the signal)."""
        return json.dumps(reminders.snapshot())

    # ------------------------------------------------------------------
    # Notes card
    # ------------------------------------------------------------------

    def _sync_notes(self):
        """Send the notes to the page when they changed."""
        if notes.version != self._last_notes_version:
            self._last_notes_version = notes.version
            self.notesChanged.emit(notes.snapshot_json())

    @Slot()
    def requestNotes(self):
        """Called by the page when it loads."""
        self._last_notes_version = notes.version
        self.notesChanged.emit(notes.snapshot_json())

    @Slot(result=str)
    def getNotes(self):
        """The page asks for the notes (backup for the signal)."""
        return notes.snapshot_json()

    @Slot()
    def closeNotes(self):
        """The X button on the notes card."""
        notes.close_card()
        self._sync_notes()

    # ------------------------------------------------------------------
    # To-do card
    # ------------------------------------------------------------------

    def _sync_todo(self):
        """Send the to-do list to the page when it changed."""
        if todos.version != self._last_todo_version:
            self._last_todo_version = todos.version
            self.todoChanged.emit(todos.snapshot_json())

    @Slot()
    def requestTodo(self):
        """Called by the page when it loads."""
        self._last_todo_version = todos.version
        self.todoChanged.emit(todos.snapshot_json())

    @Slot(result=str)
    def getTodo(self):
        """The page asks for the to-do list (backup for the signal)."""
        return todos.snapshot_json()

    @Slot()
    def closeTodo(self):
        """The X button on the card while it shows the to-do list."""
        todos.close_card()
        self._sync_todo()

    @Slot(int)
    def toggleTodo(self, number):
        """Clicking a task on the card ticks / un-ticks it."""
        todos.toggle(number)
        self._sync_todo()

    # ------------------------------------------------------------------
    # Routines card
    # ------------------------------------------------------------------

    def _sync_routines(self):
        """Send the routines to the page when they changed."""
        if routines.version != self._last_routines_version:
            self._last_routines_version = routines.version
            self.routinesChanged.emit(routines.snapshot_json())

    @Slot()
    def requestRoutines(self):
        """Called by the page when it loads."""
        self._last_routines_version = routines.version
        self.routinesChanged.emit(routines.snapshot_json())

    @Slot(result=str)
    def getRoutines(self):
        """The page asks for the routines (backup for the signal)."""
        return routines.snapshot_json()

    @Slot()
    def closeRoutines(self):
        """The X button on the card while it shows routines."""
        routines.close_card()
        self._sync_routines()

    @Slot()
    def requestReminders(self):
        """Called by the page when it loads, to get the current list."""
        self._last_reminder_version = reminders.version
        self.remindersChanged.emit(json.dumps(reminders.snapshot()))

    def _reminder_tick(self):
        # Nobody answered "What should I call this reminder?" in time.
        # The to-do card closes by itself a few seconds after a change.
        if todos.auto_hide():
            self._sync_todo()

        routines.busy()               # notices a routine question that timed out
        routines.auto_hide()
        self._sync_routines()

        expired = reminders.pop_expired_pending()
        if expired:
            message = "I didn't hear a name, so I used the default. " + expired
            self.assistantResponse.emit(message)
            self._sync_reminders()
            QTimer.singleShot(150, lambda: self._speak_message(message))

        for reminder in reminders.pop_due():
            self.reminderDue.emit(reminder["name"])
            name = reminder["name"]
            # Short delay so the page can show the alert before speech blocks.
            QTimer.singleShot(150, lambda n=name: self._speak_reminder(n))

        self._sync_reminders()

    def _speak_message(self, text):
        worker = self.voice_worker
        if worker is not None and worker.isRunning():
            worker.pause()
        try:
            speak(text)
        finally:
            if worker is not None and worker.isRunning():
                worker.resume()

    def _speak_reminder(self, name):
        worker = self.voice_worker
        if worker is not None and worker.isRunning():
            worker.pause()

        try:
            for _ in range(3):
                speak(name)
        finally:
            if worker is not None and worker.isRunning():
                worker.resume()
            self.reminderDone.emit(name)

    @Slot()
    def startListening(self):
        # Already listening continuously.
        if self.voice_worker and self.voice_worker.isRunning():
            return

        self.statusChanged.emit("LISTENING")
        self._start_worker()

    def _start_worker(self):
        self.voice_worker = VoiceWorker()
        self.voice_worker.command_ready.connect(
            self.handle_command
        )
        self.voice_worker.listening_error.connect(
            self.handle_listening_error
        )
        self.voice_worker.finished.connect(
            self.listening_finished
        )
        self.voice_worker.start()

    # ------------------------------------------------------------------
    # Earphones / microphone changes
    # ------------------------------------------------------------------

    def _audio_devices_changed(self):
        # Windows sends a burst of events: wait a moment, then reopen once.
        QTimer.singleShot(1500, self._restart_listening)

    def _restart_listening(self):
        """Stop the listening thread; listening_finished() starts a new one."""
        worker = self.voice_worker

        if worker is not None and worker.isRunning() and not worker.isInterruptionRequested():
            print("Assistant: Audio devices changed, reopening the microphone.")
            self._restart_pending = True
            worker.requestInterruption()

    def _maybe_restart_for_audio_mode(self):
        """After the robot finished speaking: apply a changed audio mode."""
        if not self._audio_restart_after_speech:
            return False

        self._audio_restart_after_speech = False
        worker = self.voice_worker

        if worker is not None and worker.isRunning():
            self._restart_pending = True
            worker.requestInterruption()
            return True

        return False

    def _arm_answer_mode(self, worker):
        """While a question is open or notes/routine are being made, the next
        phrase needs no "sudo"."""
        if worker is None or not worker.isRunning():
            return

        if reminders.has_pending():
            worker.expect_answer(PENDING_SECONDS)
        elif todos.has_pending():
            worker.expect_answer(TODO_PENDING_SECONDS)
        elif brain.has_pending():
            worker.expect_answer(brain.expect_seconds())
        elif routines.expect_seconds():
            worker.expect_answer(routines.expect_seconds())
        elif notes.active:
            worker.expect_answer(NOTE_IDLE_SECONDS)

    @Slot()
    def stopListening(self):
        self._restart_pending = False          # the user pressed Stop: do not restart

        if self.voice_worker and self.voice_worker.isRunning():
            self.voice_worker.requestInterruption()
            self.statusChanged.emit("STOPPING")

    @Slot(str)
    def handle_command(self, command):
        # Tell the page first so the working animation starts, then run the
        # command a moment later (lets the page update before we block).
        self.commandRecognized.emit(command)

        worker = self.voice_worker
        QTimer.singleShot(60, lambda: self._execute(command, worker))

    @Slot(str)
    def submitTextCommand(self, text):
        """Command typed in the search bar (text command mode)."""
        command = (text or "").strip()

        # (A typed "sudo ..." is understood by process_command itself.)
        if not command:
            self.assistantResponse.emit("Please type a command.")
            return

        self.commandRecognized.emit(command)
        QTimer.singleShot(
            60,
            lambda: self._execute(command, None, SPEAK_TEXT_REPLIES)
        )

    def _execute(self, command, worker, speak_reply=True):
        response = "Sorry, something went wrong."

        try:
            response = process_command(command)
            self.assistantResponse.emit(response)
            self._sync_reminders()
            self._sync_notes()
            self._sync_todo()
            self._sync_routines()

            # While a reminder name is expected, or while taking notes, the next
            # spoken phrase is the answer / the next point (no "sudo" needed).
            self._arm_answer_mode(worker)

            # "use headphones" / "use laptop mic": reopen the microphone
            # once the robot has finished speaking.
            if audio_devices.version != self._last_audio_version:
                self._last_audio_version = audio_devices.version
                self._audio_restart_after_speech = True

        except Exception as error:
            print("Command error:", error)
            self.assistantResponse.emit(response)

        if speak_reply:
            QTimer.singleShot(
                60,
                lambda: self._speak_response(response, worker)
            )
        else:
            self._maybe_restart_for_audio_mode()

    def _speak_response(self, response, worker):
        # Replies from the notes feature can choose what to say:
        # speech=None -> say the reply, "" -> stay silent, text -> say that.
        speech = getattr(response, "speech", None)
        quiet = speech == ""

        try:
            if speech is not None:
                if speech:
                    speak(speech)
            elif "Found " in response:
                speak("I found matching files. Check your screen.")
            elif "\n" in response:
                speak("Please check the screen for the available commands.")
            else:
                speak(response)

        finally:
            # A changed audio mode reopens the microphone now that the robot
            # has finished speaking; otherwise just listen for the next command
            # (almost immediately after a silent reply, so note points can
            # follow each other quickly).
            restarting = self._maybe_restart_for_audio_mode()

            if not restarting and worker is not None and worker.isRunning():
                worker.resume(0.15 if quiet else 0.8)

    @Slot(str)
    def handle_listening_error(self, message):
        self.assistantResponse.emit(message)

    def listening_finished(self):
        self.voice_worker = None

        # Earphones connected / disconnected: carry on with the right microphone.
        if self._restart_pending:
            self._restart_pending = False
            self._start_worker()
            self._arm_answer_mode(self.voice_worker)
            return

        self.statusChanged.emit("ONLINE")