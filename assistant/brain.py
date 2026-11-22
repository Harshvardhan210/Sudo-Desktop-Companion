"""
The assistant's brain: it learns from you. No GPT, no Ollama, no online model.
Everything it learns is saved in the project's  brain/  folder:

    brain/intents.json   what each action is called + example sentences (it retrains from these)
    brain/qa.json        questions you taught it and their answers
    brain/memory.json    your nicknames ("call chrome browser") and how often you use each action
    brain/unknown.txt    every message it did not understand

How it understands a sentence (no regex needed):
    1. It compares the words with the example sentences of every action
       (word + word-pair matching, TF-IDF cosine similarity, pure Python).
    2. Words it has never seen in the examples are treated as the "thing"
       (app name, search text, ...), so "launch the postman app" works.
    3. Sure about the action  -> it does it.
       Not sure              -> it asks which action you meant, saves your answer
                                as a new example and retrains immediately.
       No idea, a question   -> it asks what to answer and saves it in qa.json.

You can say:
    call chrome browser        (or "call chrome as browser")  -> a nickname
    forget <question>          forget alias <name>
    show aliases / my habits / what have you learned
"""

import difflib
import json
import math
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from features.notes import Reply


# <project>/brain/   (this file lives in <project>/assistant/)
BRAIN_DIR = Path(__file__).resolve().parent.parent / "brain"

SLOT = "{slot}"

# How sure it must be
HIGH = 0.70        # at or above this (and clearly ahead of the others): just do it
MARGIN = 0.10      # how far the best action must be ahead of the second best
LOW = 0.35         # below this it has no idea what you meant

PENDING_SECONDS = 30
MAX_QA = 500
MAX_COMMAND_COUNTS = 200

STOPWORDS = {
    "the", "a", "an", "my", "me", "please", "pls", "plz", "for", "you", "your", "can",
    "could", "would", "will", "kindly", "to", "of", "is", "are", "it", "this", "that",
    "i", "we", "do", "does", "just", "now", "some", "any", "hey", "ok", "okay", "so",
    "and", "or", "be", "am", "let", "lets", "wanna", "hai", "hain", "ko", "ka", "ki",
    "ke", "se", "par", "pe", "mein", "me", "kar", "karo", "kro", "kijiye", "kariye",
    "kare", "de", "dijiye", "zara", "jara", "mujhe", "mera", "meri", "aap", "tum",
    "sudo",
}

# Words next to the "thing" that are not part of its name
NOISE = {
    "app", "application", "program", "website", "site", "folder", "window", "windows",
    "software", "please", "pls", "now", "for", "me", "the", "my", "a", "an",
}

QUESTION_WORDS = {
    "what", "whats", "who", "whom", "whose", "when", "where", "why", "how", "which",
    "is", "are", "am", "do", "does", "did", "can", "could", "will", "would", "should",
    "tell", "explain", "define", "kya", "kaun", "kab", "kahan", "kyun", "kyon", "kaise",
    "kitna", "kitne", "kaisa",
}

CANCEL_WORDS = {"skip", "cancel", "never mind", "nevermind", "ignore", "forget it", "stop"}
NO_WORDS = {"none", "no", "nope", "nahi", "neither", "none of these", "none of them"}
YES_WORDS = {"yes", "yeah", "yep", "haan", "han", "ha", "correct", "right", "ok", "okay", "sure"}

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "ek": 1, "do": 2, "teen": 3, "first": 1, "second": 2,
    "third": 3, "pehla": 1, "doosra": 2, "teesra": 3,
}

# Aliases are only used in commands that start with one of these words
ALIAS_COMMAND_WORDS = {"open", "launch", "start", "run", "close", "quit", "exit", "play", "show", "visit", "go", "browse"}
RESERVED_ALIAS_NAMES = ALIAS_COMMAND_WORDS | {
    "add", "delete", "remove", "clear", "find", "search", "google", "remind", "mute",
    "unmute", "volume", "lock", "help", "hello", "note", "notes", "task", "tasks",
}


# Actions it knows at the start. It saves them to intents.json and adds to them as you teach it.
DEFAULT_INTENTS = {
    "open_app": {"label": "open an app, website or folder", "command": "open {slot}", "examples": [
        "open {slot}", "launch {slot}", "start {slot}", "run {slot}", "open {slot} app",
        "launch {slot} application", "i want to open {slot}", "can you open {slot} for me",
        "{slot} kholo", "{slot} open karo", "{slot} chalu karo"]},
    "close_app": {"label": "close an app", "command": "close {slot}", "examples": [
        "close {slot}", "quit {slot}", "exit {slot}", "close {slot} app", "{slot} band karo",
        "{slot} close karo"]},
    "volume_up": {"label": "turn the volume up", "command": "volume up", "examples": [
        "volume up", "increase volume", "increase the volume", "turn up the volume",
        "raise the volume", "make it louder", "louder", "sound up", "turn it up",
        "volume badhao", "awaaz badhao", "awaaz tez karo"]},
    "volume_down": {"label": "turn the volume down", "command": "volume down", "examples": [
        "volume down", "decrease volume", "decrease the volume", "turn down the volume",
        "lower the volume", "make it quieter", "quieter", "softer", "turn it down",
        "volume kam karo", "awaaz kam karo"]},
    "mute": {"label": "mute the sound", "command": "mute volume", "examples": [
        "mute", "mute volume", "mute the sound", "silence", "awaaz band karo"]},
    "unmute": {"label": "unmute the sound", "command": "unmute volume", "examples": [
        "unmute", "unmute volume", "unmute the sound", "sound on", "turn the sound back on",
        "awaaz chalu karo"]},
    "get_time": {"label": "tell the time", "command": "what time is it", "examples": [
        "what time is it", "tell me the time", "current time", "time please", "whats the time",
        "what is the time", "time batao", "kitne baje hain"]},
    "get_date": {"label": "tell today's date", "command": "what is today's date", "examples": [
        "what is todays date", "todays date", "what day is it", "tell me the date",
        "what is the date today", "whats the date", "aaj ki date batao"]},
    "battery_status": {"label": "check the battery", "command": "battery status", "examples": [
        "battery status", "check battery", "how much battery is left", "battery level",
        "battery percentage", "battery kitni hai"]},
    "google_search": {"label": "search the web", "command": "google {slot}", "examples": [
        "google {slot}", "search the web for {slot}", "search google for {slot}",
        "look up {slot}", "web search {slot}", "search online for {slot}",
        "search internet for {slot}", "{slot} google karo"]},
    "youtube_play": {"label": "play a video or song", "command": "play {slot}", "examples": [
        "play {slot}", "play {slot} on youtube", "put on {slot}", "i want to listen to {slot}",
        "i want to watch {slot}", "{slot} bajao"]},
    "youtube_search": {"label": "search YouTube", "command": "search youtube for {slot}", "examples": [
        "search youtube for {slot}", "find {slot} on youtube", "search {slot} on youtube",
        "youtube search {slot}"]},
    "file_search": {"label": "find a file", "command": "find {slot}", "examples": [
        "find {slot}", "find my {slot}", "where is {slot}", "where is my {slot}",
        "search my files for {slot}", "locate {slot}", "look for {slot} file", "{slot} dhundo"]},
    "add_task": {"label": "add a to-do task", "command": "add task {slot}", "examples": [
        "add task {slot}", "add a task {slot}", "add a task to {slot}",
        "add {slot} to my to do list", "new task {slot}", "create task {slot}", "todo {slot}"]},
    "show_tasks": {"label": "show my to-do list", "command": "show my tasks", "examples": [
        "show my tasks", "show tasks", "what is on my to do list", "open my to do list",
        "list my tasks", "tasks dikhao"]},
    "add_note": {"label": "take a note", "command": "add a note", "examples": [
        "add a note", "take a note", "write a note", "new note", "start taking notes",
        "note likho"]},
    "open_notes": {"label": "open my notes", "command": "open notes", "examples": [
        "open notes", "show my notes", "open my notes", "show notes", "notes dikhao"]},
    "save_notes": {"label": "save my notes", "command": "save notes", "examples": [
        "save notes", "save my notes", "finish notes", "notes save karo"]},
    "set_reminder": {"label": "set a reminder", "command": "remind me in {slot}", "examples": [
        "remind me in {slot}", "set a reminder for {slot}", "set a reminder in {slot}",
        "set reminder for {slot}", "remind me after {slot}", "create a reminder in {slot}"]},
    "run_routine": {"label": "run a routine", "command": "run {slot} routine", "examples": [
        "run {slot} routine", "start {slot} routine", "begin {slot} routine",
        "run my {slot} routine", "open {slot} routine", "{slot} routine shuru karo"]},
    "lock_computer": {"label": "lock the computer", "command": "lock computer", "examples": [
        "lock computer", "lock my pc", "lock the screen", "lock my laptop", "lock pc",
        "computer lock karo"]},
    "greeting": {"label": "say hello", "command": "hello", "examples": [
        "hello", "hi", "hey there", "good morning", "good evening", "namaste"]},
    "help": {"label": "show what I can do", "command": "help", "examples": [
        "help", "what can you do", "show commands", "what are your commands", "madad karo"]},
    "change_voice": {"label": "change my voice", "command": "change voice", "examples": [
        "change voice", "change your voice", "use a different voice", "switch voice",
        "next voice"]},
    "speak_faster": {"label": "speak faster", "command": "speak faster", "examples": [
        "speak faster", "talk faster", "speed up your voice", "tez bolo"]},
    "speak_slower": {"label": "speak slower", "command": "speak slower", "examples": [
        "speak slower", "talk slower", "slow down your voice", "dheere bolo"]},
}


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

_STEM_MAP = {
    "taking": "take", "writing": "write", "opening": "open", "closing": "close",
    "playing": "play", "starting": "start", "running": "run", "launching": "launch",
    "searching": "search", "muting": "mute", "locking": "lock", "watching": "watch",
    "listening": "listen", "increasing": "increase", "decreasing": "decrease",
}

_PUNCTUATION = re.compile(r"[^\w\s\u0900-\u097F]|[\u0964\u0965]")


def words_of(text):
    """Lowercase words of a sentence ("{slot}" is kept as one word)."""

    text = text.lower().replace(SLOT, " slotmark ").replace("'", "").replace("’", "")
    words = _PUNCTUATION.sub(" ", text).split()
    return [SLOT if w == "slotmark" else w for w in words]


def stem(word):
    if word in _STEM_MAP:
        return _STEM_MAP[word]
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
        return True
    except OSError as error:
        print("Brain: could not save", path.name, "-", error)
        return False


def is_question(text):
    words = words_of(text)
    return bool(words) and ("?" in text or words[0] in QUESTION_WORDS)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _speak_number_word(n):
    return {1: "one", 2: "two", 3: "three"}.get(n, str(n))


class Match:
    def __init__(self, intent, score, second, example, utterance_words):
        self.intent = intent
        self.score = score
        self.second = second
        self.example = example                # the best matching example (words, with {slot})
        self.words = utterance_words
        self.confident = score >= HIGH and (score - second) >= MARGIN


# ----------------------------------------------------------------------------
# The trainable model (TF-IDF cosine similarity over example sentences)
# ----------------------------------------------------------------------------

class _Model:
    """Built from intents.json. It is rebuilt (retrained) every time you teach it something."""

    def __init__(self, intents):
        self.intents = intents
        self.function_words = set()
        self.examples = []        # (intent, words, feature vector)
        self.slot_position = {}   # intent -> "start" / "end" / "middle" (where the thing usually is)

        entries = []

        for intent, data in intents.items():
            positions = Counter()

            for example in data.get("examples", []):
                words = words_of(example)
                if not words:
                    continue

                for w in words:
                    if w != SLOT and w not in STOPWORDS:
                        self.function_words.add(stem(w))

                if SLOT in words:
                    index = words.index(SLOT)
                    positions["start" if index == 0 else "end" if index == len(words) - 1 else "middle"] += 1

                entries.append((intent, words))

            if positions:
                self.slot_position[intent] = positions.most_common(1)[0][0]

        feature_lists = [self._example_features(words) for _, words in entries]

        document_frequency = Counter()
        for features in feature_lists:
            document_frequency.update(set(features))

        total = len(feature_lists) or 1
        self.idf = {f: math.log((total + 1) / (df + 1)) + 1 for f, df in document_frequency.items()}
        self.default_idf = math.log(total + 1) + 1

        for (intent, words), features in zip(entries, feature_lists):
            self.examples.append((intent, words, self._vector(features)))

    # ---- features ----

    @staticmethod
    def _with_pairs(tokens):
        features = list(tokens)
        features += [tokens[i] + " " + tokens[i + 1] for i in range(len(tokens) - 1)]

        if tokens and tokens[0] == "<slot>":
            features.append("^<slot>")
        if tokens and tokens[-1] == "<slot>":
            features.append("<slot>$")

        return features

    def _example_features(self, words):
        tokens = []

        for w in words:
            if w == SLOT:
                if not tokens or tokens[-1] != "<slot>":
                    tokens.append("<slot>")
            elif w not in STOPWORDS:
                tokens.append(stem(w))

        return self._with_pairs(tokens)

    def utterance_features(self, words):
        """Words that never appear in the examples are "the thing" (the slot)."""

        kinds = []   # "stop", "known" or "slot" for each word

        for w in words:
            if w in STOPWORDS:
                kinds.append("stop")
            elif stem(w) in self.function_words:
                kinds.append("known")
            else:
                kinds.append("slot")

        # Stop words between two unknown words belong to the same thing ("romeo and juliet")
        slot_positions = [i for i, k in enumerate(kinds) if k == "slot"]
        for a, b in zip(slot_positions, slot_positions[1:]):
            if all(kinds[i] == "stop" for i in range(a + 1, b)):
                for i in range(a + 1, b):
                    kinds[i] = "slot"

        tokens = []

        for w, kind in zip(words, kinds):
            if kind == "known":
                tokens.append(stem(w))
            elif kind == "slot" and (not tokens or tokens[-1] != "<slot>"):
                tokens.append("<slot>")

        return self._with_pairs(tokens)

    def _vector(self, features):
        counts = Counter(features)
        vector = {f: c * self.idf.get(f, self.default_idf) for f, c in counts.items()}
        norm = math.sqrt(sum(v * v for v in vector.values())) or 1.0
        return {f: v / norm for f, v in vector.items()}

    # ---- classification ----

    def classify(self, words):
        features = self.utterance_features(words)

        if not features:
            return None

        vector = self._vector(features)
        best = {}

        for intent, example_words, example_vector in self.examples:
            score = sum(weight * example_vector.get(f, 0.0) for f, weight in vector.items())
            if score > best.get(intent, (0.0, None))[0]:
                best[intent] = (score, example_words)

        if not best:
            return None

        ranked = sorted(best.items(), key=lambda item: item[1][0], reverse=True)
        intent, (score, example) = ranked[0]
        second = ranked[1][1][0] if len(ranked) > 1 else 0.0

        match = Match(intent, score, second, example, words)
        match.ranked = [(name, data[0]) for name, data in ranked]
        return match

    # ---- the thing (slot) ----

    def extract_slot(self, words, example):
        """The app name / search text / ... that is left after the action words."""

        if SLOT not in example:
            return ""

        index = example.index(SLOT)
        prefix, suffix = example[:index], example[index + 1:]
        position = "start" if index == 0 else "end" if index == len(example) - 1 else "middle"

        def is_function(word):
            return word in STOPWORDS or stem(word) in self.function_words

        tokens = list(words)

        # Cut the fixed words of the example off the end ("... on youtube", "... kholo")
        if suffix:
            n = len(suffix)
            if [stem(t) for t in tokens[-n:]] == [stem(t) for t in suffix]:
                tokens = tokens[:-n]

        if position in ("end", "middle"):
            while tokens and is_function(tokens[0]):
                tokens.pop(0)
        if position == "start":
            while tokens and is_function(tokens[-1]):
                tokens.pop()

        while tokens and tokens[-1] in NOISE:
            tokens.pop()
        while tokens and tokens[0] in NOISE:
            tokens.pop(0)

        return " ".join(tokens)

    def slot_run(self, words, position):
        """Find "the thing" inside a new sentence (to turn it into an example)."""

        candidates = [i for i, w in enumerate(words)
                      if w not in STOPWORDS and w != SLOT and stem(w) not in self.function_words]

        # Merge unknown words that are only separated by stop words
        runs = []
        for i in candidates:
            if runs and all(words[j] in STOPWORDS for j in range(runs[-1][-1] + 1, i)):
                runs[-1] = list(range(runs[-1][0], i + 1))
            else:
                runs.append([i])

        if not runs:
            non_stop = [i for i, w in enumerate(words) if w not in STOPWORDS]
            if not non_stop:
                return None
            index = non_stop[0] if position == "start" else non_stop[-1]
            return index, index

        run = runs[0] if position == "start" else runs[-1]
        return run[0], run[-1]


# ----------------------------------------------------------------------------
# The brain
# ----------------------------------------------------------------------------

class Brain:

    def __init__(self, directory=BRAIN_DIR):
        self.dir = Path(directory)
        self.intents_file = self.dir / "intents.json"
        self.qa_file = self.dir / "qa.json"
        self.memory_file = self.dir / "memory.json"
        self.unknown_file = self.dir / "unknown.txt"

        self._lock = threading.RLock()
        self.pending = None            # a question the brain asked and waits for the answer to

        self._load_all()

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    def _load_all(self):
        self.intents = self._load_intents()
        self.qa = _read_json(self.qa_file, {})
        self.memory = _read_json(self.memory_file, {})

        if not isinstance(self.qa, dict):
            self.qa = {}
        if not isinstance(self.memory, dict):
            self.memory = {}

        self.memory.setdefault("aliases", {})
        self.memory.setdefault("usage", {})
        self.memory["usage"].setdefault("actions", {})
        self.memory["usage"].setdefault("commands", {})
        self.memory.setdefault("learned_examples", 0)

        # Create the files so you can open and edit them
        if not self.qa_file.exists():
            _write_json(self.qa_file, self.qa)
        if not self.memory_file.exists():
            _write_json(self.memory_file, self.memory)

        self.train()

    def _load_intents(self):
        data = _read_json(self.intents_file, None)

        valid = isinstance(data, dict) and data and all(
            isinstance(v, dict) and isinstance(v.get("command"), str) and isinstance(v.get("examples"), list)
            for v in data.values()
        )

        if valid:
            return data

        if self.intents_file.exists():
            # Do not destroy a file you edited by hand and broke: keep a copy.
            backup = self.intents_file.with_name("intents.broken.json")
            try:
                os.replace(self.intents_file, backup)
                print(f"Brain: intents.json was not valid, saved a copy as {backup.name} and restored the defaults.")
            except OSError:
                pass

        intents = json.loads(json.dumps(DEFAULT_INTENTS))
        _write_json(self.intents_file, intents)
        return intents

    def _save_intents(self):
        _write_json(self.intents_file, self.intents)

    def _save_qa(self):
        _write_json(self.qa_file, self.qa)

    def _save_memory(self):
        self.memory["updated"] = _now()
        _write_json(self.memory_file, self.memory)

    def log_unknown(self, text):
        """Add a message it did not understand to unknown.txt."""

        text = " ".join(str(text).split())
        if not text:
            return

        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.unknown_file.open("a", encoding="utf-8") as file:
                file.write(f"{_now()} | {text}\n")
        except OSError as error:
            print("Brain: could not write unknown.txt -", error)

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(self):
        """(Re)build the model from intents.json. Takes a few milliseconds."""

        self._model = _Model(self.intents)

    def classify(self, text):
        model = self._model
        return model.classify(words_of(text))

    def build_command(self, intent, text, example=None):
        """Turn a sentence into the command the assistant runs, e.g. "open notepad"."""

        model = self._model
        template = self.intents[intent]["command"]

        if SLOT not in template:
            return template

        words = words_of(text)

        if example is None:
            match = model.classify(words)
            example = match.example if match and match.intent == intent else None

        if example is None:
            examples = [w for i, w, _ in model.examples if i == intent and SLOT in w]
            example = examples[0] if examples else None

        slot = model.extract_slot(words, example) if example else ""

        if not slot:
            return None

        return template.replace(SLOT, slot)

    def learn_example(self, intent, text):
        """Save a new example sentence for an action and retrain immediately."""

        with self._lock:
            model = self._model
            words = words_of(text)
            template = self.intents[intent]["command"]

            if SLOT in template:
                position = model.slot_position.get(intent, "end")
                found = model.slot_run(words, position)

                if found:
                    first, last = found
                    words = words[:first] + [SLOT] + words[last + 1:]

            example = " ".join(words)

            if example not in self.intents[intent]["examples"]:
                self.intents[intent]["examples"].append(example)
                self.memory["learned_examples"] = self.memory.get("learned_examples", 0) + 1
                self.memory.setdefault("learned", []).append(
                    {"intent": intent, "example": example, "when": _now()}
                )
                self._save_intents()
                self._save_memory()

            self.train()
            return example

    # ------------------------------------------------------------------
    # Questions and answers (qa.json)
    # ------------------------------------------------------------------

    _QA_PREFIX = re.compile(
        r"^(?:(?:please|pls|hey|ok|okay)\s+)*(?:(?:can|could|would) you\s+)?"
        r"(?:tell me|do you know|i want to know|i wanna know|let me know)\s+"
    )

    @classmethod
    def _qa_key(cls, text):
        words = [w for w in words_of(text) if w not in {"please", "pls", "plz", "sudo"}]
        key = cls._QA_PREFIX.sub("", " ".join(words))
        return " ".join(w for w in key.split() if w not in {"hey", "ok", "okay"})

    def _qa_find(self, text, threshold=0.88):
        key = self._qa_key(text)

        if not key:
            return None, None

        if key in self.qa:
            return key, self.qa[key]

        best_key, best_score = None, 0.0
        key_words = {w for w in key.split() if w not in STOPWORDS}

        for other in self.qa:
            ratio = difflib.SequenceMatcher(None, key, other).ratio()
            other_words = {w for w in other.split() if w not in STOPWORDS}
            overlap = (len(key_words & other_words) / len(key_words | other_words)
                       if key_words and other_words else 0.0)

            score = max(ratio, overlap if len(key_words) >= 2 else 0.0)

            if score >= threshold and score > best_score:
                best_key, best_score = other, score

        return (best_key, self.qa[best_key]) if best_key else (None, None)

    def answer_for(self, text):
        key, entry = self._qa_find(text)

        if not entry:
            return None

        entry["asked"] = entry.get("asked", 0) + 1
        self._save_qa()
        self._count("qa", text)
        return entry["answer"]

    def save_answer(self, question, answer):
        key = self._qa_key(question)

        if len(self.qa) >= MAX_QA and key not in self.qa:
            return False

        self.qa[key] = {
            "question": " ".join(question.split()),
            "answer": " ".join(answer.split()),
            "asked": 0,
            "added": _now(),
        }
        self._save_qa()
        return True

    def forget_answer(self, text):
        """Delete a taught answer. Returns the forgotten question, or None."""

        key, entry = self._qa_find(text, threshold=0.75)

        if not entry:
            return None

        del self.qa[key]
        self._save_qa()
        return entry["question"]

    def forget_lesson(self, text):
        """Delete a sentence you taught for an action (if a lesson was wrong)."""

        said = " ".join(words_of(text))

        for item in list(self.memory.get("learned", [])):
            example = item["example"]
            pattern = "^" + re.escape(example).replace(re.escape(SLOT), ".+") + "$"

            if re.match(pattern, said) or said == example:
                examples = self.intents.get(item["intent"], {}).get("examples", [])

                if example in examples:
                    examples.remove(example)

                self.memory["learned"].remove(item)
                self.memory["learned_examples"] = max(0, self.memory.get("learned_examples", 1) - 1)
                self._save_intents()
                self._save_memory()
                self.train()
                return example.replace(SLOT, "...")

        return None

    # ------------------------------------------------------------------
    # Memory: nicknames and habits (memory.json)
    # ------------------------------------------------------------------

    def _known_targets(self):
        names = set()

        try:
            from features.app_closer import APP_ALIASES
            names.update(APP_ALIASES)
        except Exception:
            pass

        try:
            from features.web_tools import KNOWN_SITES
            names.update(KNOWN_SITES)
        except Exception:
            pass

        try:
            from features.routines import routines
            names.update(routines.routines)
            names.update(f"{n} routine" for n in routines.routines)
        except Exception:
            pass

        names.update({"desktop", "documents", "downloads", "pictures", "music", "videos"})
        names.update(self.memory["aliases"].values())
        return {n.lower() for n in names}

    def set_alias(self, target, name):
        target = " ".join(words_of(target))
        name = " ".join(words_of(name))

        # "call chrome browser" when "chrome" is itself a nickname: use what it stands for
        target = self.memory["aliases"].get(target, target)

        if not target or not name:
            return "Please say it like: call chrome as browser."

        if name == target:
            return "That is already its name."

        if name in RESERVED_ALIAS_NAMES or name in self._known_targets() - set(self.memory["aliases"]):
            return f"{name} already means something to me, so I can't use it as a nickname."

        if len(name) > 30:
            return "That nickname is too long."

        self.memory["aliases"][name] = target
        self._save_memory()
        return f"Okay, from now on {name} means {target}."

    def remove_alias(self, name):
        name = " ".join(words_of(name))

        if name in self.memory["aliases"]:
            target = self.memory["aliases"].pop(name)
            self._save_memory()
            return f"Forgot the nickname {name} (it meant {target})."

        return f"I don't have a nickname called {name}."

    def expand_aliases(self, text):
        """"open browser" -> "open chrome" (only in commands about apps, sites and routines)."""

        aliases = self.memory["aliases"]
        words = words_of(text)

        if not aliases or not words or words[0] not in ALIAS_COMMAND_WORDS:
            return text

        pattern = re.compile(
            r"(?<!\w)(" + "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True)) + r")(?!\w)",
            re.I,
        )

        return pattern.sub(lambda m: aliases[m.group(1).lower()], text)

    def _count(self, action, text):
        usage = self.memory["usage"]
        usage["actions"][action] = usage["actions"].get(action, 0) + 1

        key = " ".join(words_of(text))
        if key:
            commands = usage["commands"]
            commands[key] = commands.get(key, 0) + 1

            if len(commands) > MAX_COMMAND_COUNTS:       # keep only the most used
                for name, _ in sorted(commands.items(), key=lambda kv: kv[1])[:20]:
                    del commands[name]

        self._save_memory()

    def record_usage(self, text):
        """Count one use of the action behind this command (habits)."""

        match = self.classify(text)

        if match and match.confident:
            self._count(match.intent, text)

    def habits(self):
        usage = self.memory["usage"]
        actions = sorted(usage["actions"].items(), key=lambda kv: kv[1], reverse=True)[:5]
        commands = sorted(usage["commands"].items(), key=lambda kv: kv[1], reverse=True)[:5]

        if not actions:
            return Reply("I haven't counted anything yet. Use me for a while and ask again.")

        def label(name):
            if name == "qa":
                return "answer a question you taught me"
            return self.intents.get(name, {}).get("label", name.replace("_", " "))

        def times(n):
            return f"{n} time" if n == 1 else f"{n} times"

        lines = ["Your most used actions:"]
        lines += [f"{i}. {label(n)}: {times(c)}" for i, (n, c) in enumerate(actions, 1)]

        if commands:
            lines.append("\nYour most used commands:")
            lines += [f"{i}. {n}: {times(c)}" for i, (n, c) in enumerate(commands, 1)]

        top = actions[0]
        speech = f"You use {label(top[0])} the most, {times(top[1])}."
        if len(actions) > 1:
            speech += f" Then {label(actions[1][0])}, {times(actions[1][1])}."

        return Reply("\n".join(lines), speech=speech)

    def aliases_text(self):
        aliases = self.memory["aliases"]

        if not aliases:
            return Reply("You have no nicknames yet. Say: call chrome as browser.")

        lines = ["Your nicknames:"] + [f"- {a} = {t}" for a, t in aliases.items()]
        return Reply("\n".join(lines), speech=f"You have {len(aliases)} nickname{'s' if len(aliases) != 1 else ''}.")

    def learned_text(self):
        examples = self.memory.get("learned_examples", 0)
        total = sum(len(i["examples"]) for i in self.intents.values())

        try:
            unknown = sum(1 for _ in self.unknown_file.open(encoding="utf-8"))
        except OSError:
            unknown = 0

        def count(n, word):
            return f"{n} {word}" if n == 1 else f"{n} {word}s"

        text = (
            f"I know {len(self.intents)} actions from {count(total, 'example sentence')} "
            f"({examples} taught by you), {count(len(self.qa), 'answer')}, "
            f"{count(len(self.memory['aliases']), 'nickname')}, and I have written down "
            f"{count(unknown, 'message')} I didn't understand."
        )
        return Reply(text)

    # ------------------------------------------------------------------
    # Commands about the brain itself
    # ------------------------------------------------------------------

    _CALL_AS = re.compile(r"^call\s+(.+?)\s+as\s+(.+)$", re.I)
    _CALL = re.compile(r"^call\s+(.+)$", re.I)
    _FORGET_ALIAS = re.compile(r"^forget\s+(?:the\s+)?(?:alias|nickname)\s+(.+)$", re.I)
    _FORGET = re.compile(r"^forget\s+(?:the\s+answer\s+(?:to|for)\s+)?(.+)$", re.I)
    _SHOW_ALIASES = re.compile(r"^(?:show|list|what are)\s+(?:my\s+)?(?:aliases|nicknames)$|^my\s+(?:aliases|nicknames)$", re.I)
    _HABITS = re.compile(
        r"^(?:show\s+)?(?:my\s+)?(?:habits|usage)$|^what do i (?:use|do) (?:the\s+)?most$|"
        r"^what (?:are )?my (?:habits|most used\b.*)$|^(?:show\s+)?my usage (?:habits|stats)$", re.I)
    _LEARNED = re.compile(r"^what (?:have|did) you (?:learned|learnt|learn|learned)$|^show what you(?:'ve| have) learned$", re.I)

    def handle_management(self, text):
        """call X Y / forget ... / my habits / show aliases. Returns a Reply or None."""

        text = text.strip().rstrip(".?!").strip()

        if not text:
            return None

        match = self._FORGET_ALIAS.match(text)
        if match:
            return Reply(self.remove_alias(match.group(1)))

        if self._SHOW_ALIASES.match(text):
            return self.aliases_text()

        if self._HABITS.match(text):
            return self.habits()

        if self._LEARNED.match(text):
            return self.learned_text()

        match = self._FORGET.match(text)
        if match:
            removed = self.forget_answer(match.group(1))
            if removed:
                return Reply(f"Okay, I forgot the answer to: {removed}", speech="Okay, I forgot that.")

            lesson = self.forget_lesson(match.group(1))
            if lesson:
                return Reply(f'Okay, I forgot that "{lesson}" is an action.', speech="Okay, I forgot that.")

            return Reply("I don't have a learned answer for that.")

        match = self._CALL_AS.match(text)
        if match:
            return Reply(self.set_alias(match.group(1), match.group(2)))

        match = self._CALL.match(text)
        if match:
            return Reply(self._call_without_as(match.group(1)))

        return None

    def _call_without_as(self, body):
        tokens = words_of(body)
        known = self._known_targets()

        # "call vs code editor": the longest start that is an app, site or routine I know
        for n in range(len(tokens) - 1, 0, -1):
            if " ".join(tokens[:n]) in known:
                return self.set_alias(" ".join(tokens[:n]), " ".join(tokens[n:]))

        if len(tokens) == 2:
            return self.set_alias(tokens[0], tokens[1])

        return "Please say it like: call chrome as browser."

    # ------------------------------------------------------------------
    # Conversation (the brain asks you a question and waits)
    # ------------------------------------------------------------------

    def has_pending(self):
        if self.pending and time.time() > self.pending["until"]:
            self.pending = None
        return self.pending is not None

    def expect_seconds(self):
        return PENDING_SECONDS if self.has_pending() else 0

    @staticmethod
    def _ask(text, speech=None):
        reply = Reply(text, speech=speech)
        reply.brain_ask = True
        return reply

    def _label(self, intent):
        return self.intents[intent].get("label", intent.replace("_", " "))

    def _ask_which_intent(self, text, match, unsure):
        ranked = [name for name, _ in getattr(match, "ranked", [])][:3] if match else []

        if len(ranked) < 3:
            for name in ("open_app", "google_search", "youtube_play"):
                if name not in ranked and name in self.intents:
                    ranked.append(name)
                ranked = ranked[:3]

        self.pending = {"kind": "intent", "text": text, "options": ranked,
                        "until": time.time() + PENDING_SECONDS, "tries": 0}

        lines = [f'I\'m not sure what you meant by "{text}". Which action was it?' if unsure
                 else f'I don\'t understand "{text}" yet. Which action is it?']
        lines += [f"{i}. {self._label(name)}" for i, name in enumerate(ranked, 1)]
        lines.append("Say a number or the action name, or say skip.")

        speech = ("I'm not sure what you meant. " if unsure else "I don't understand that yet. ") + "Is it " + ", ".join(
            f"{_speak_number_word(i)}, {self._label(name)}" for i, name in enumerate(ranked, 1)
        ) + "? Say a number, or skip."

        return self._ask("\n".join(lines), speech)

    def _ask_for_answer(self, text):
        self.pending = {"kind": "answer", "text": text, "until": time.time() + PENDING_SECONDS}

        return self._ask(
            f'I don\'t know the answer to that yet. What should I say when you ask "{text}"? (Say skip to ignore.)',
            "I don't know that yet. What should I answer?",
        )

    def handle_unknown(self, text, run):
        """
        A sentence the normal commands did not understand.
        run(command) runs a command and returns its reply.
        """

        text = " ".join(str(text).split())

        if not text:
            return Reply("Sorry, I don't understand that command.")

        # 1. A question you taught me before?
        answer = self.answer_for(text)
        if answer:
            return Reply(answer)

        # 2. Which action does it sound like?
        match = self.classify(text)

        if match and match.confident:
            command = self.build_command(match.intent, text, match.example)

            if command:
                return run(command)

            # Sure about the action, but I could not find the app name / text
            self.log_unknown(text)
            return Reply(f"I understood you want to {self._label(match.intent)}, but what exactly? Please say it again with the name.")

        # 3. Not sure: a question you never taught me, or an unclear command
        self.log_unknown(text)

        if is_question(text):
            return self._ask_for_answer(text)

        if match and match.score >= LOW:
            return self._ask_which_intent(text, match, unsure=True)

        return self._ask_which_intent(text, match, unsure=False)

    def handle_pending(self, answer, run):
        """The reply to a question the brain asked."""

        pending, self.pending = self.pending, None
        answer = " ".join(str(answer).split())
        lowered = " ".join(words_of(answer))

        if not pending:
            return Reply("Sorry, I don't understand that command.")

        if pending["kind"] == "answer":
            if lowered in CANCEL_WORDS or not answer:
                return Reply("Okay, I won't save an answer.")

            if self.save_answer(pending["text"], answer):
                return Reply("Got it. I'll remember that.")

            return Reply("My list of answers is full. Forget some with: forget <question>.")

        # kind == "intent"
        if lowered in CANCEL_WORDS or lowered in NO_WORDS:
            if lowered in NO_WORDS:
                self.log_unknown(pending["text"])
            return Reply("Okay, I'll skip it.")

        intent = self._choose_intent(lowered, pending["options"])

        if intent is None:
            pending["tries"] += 1
            if pending["tries"] >= 2:
                self.log_unknown(pending["text"])
                return Reply("Sorry, I couldn't tell which action you meant. Let's skip it.")

            pending["until"] = time.time() + PENDING_SECONDS
            self.pending = pending
            return self._ask("Sorry, I didn't catch that. Say a number or the action name, or skip.")

        # Learn the sentence and retrain right away
        example = self.learn_example(intent, pending["text"])
        command = self.build_command(intent, pending["text"])

        learned = f'Learned: "{pending["text"]}" means {self._label(intent)}.'

        if not command:
            return Reply(learned + " Tell me again with the name and I'll do it.")

        result = run(command)
        result_text = str(result)
        result_speech = getattr(result, "speech", None)

        combined = Reply(
            f"{learned}\n{result_text}",
            speech=("Got it. " + (result_speech if result_speech is not None else result_text.splitlines()[0])).strip(),
        )

        return combined

    def _choose_intent(self, lowered, options):
        if lowered in YES_WORDS and options:
            return options[0]

        for word in lowered.split():
            number = int(word) if word.isdigit() else NUMBER_WORDS.get(word)
            if number and 1 <= number <= len(options):
                return options[number - 1]

        # By name: "open app", "play video", "volume up"
        best, best_score = None, 0.0
        said = set(lowered.split())

        for name, data in self.intents.items():
            candidates = [name.replace("_", " "), data.get("label", ""), data.get("command", "").replace(SLOT, "").strip()]

            for candidate in candidates:
                cand_words = {w for w in words_of(candidate) if w not in STOPWORDS}
                if not cand_words:
                    continue

                if lowered == " ".join(words_of(candidate)):
                    return name

                score = len(said & cand_words) / len(said | cand_words)
                if score > best_score:
                    best, best_score = name, score

        return best if best_score >= 0.5 else None

    # ------------------------------------------------------------------
    # For "can I say this without sudo?"
    # ------------------------------------------------------------------

    def recognizes(self, text):
        """True if I would clearly understand this (a taught question, or a sure action)."""

        _, entry = self._qa_find(text)

        if entry:
            return True

        match = self.classify(text)
        return bool(match and match.confident)


# One shared brain used by the whole app
brain = Brain()