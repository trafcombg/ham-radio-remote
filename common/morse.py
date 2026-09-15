"""Morse code table and text -> timed key-on/off segment encoder."""

MORSE_TABLE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "/": "-..-.", "=": "-...-",
}


def dit_ms(wpm: int) -> float:
    """Standard PARIS timing: one dit = 1200/WPM milliseconds."""
    return 1200.0 / wpm


def encode_text(text: str, wpm: int) -> list[tuple[bool, float]]:
    """Text -> [(key_on, duration_ms), ...], including intra-character,
    inter-character and inter-word gaps. Unknown characters are skipped."""
    dit = dit_ms(wpm)
    segments: list[tuple[bool, float]] = []
    words = [w for w in text.upper().split(" ") if w]
    for wi, word in enumerate(words):
        for li, letter in enumerate(word):
            code = MORSE_TABLE.get(letter)
            if not code:
                continue
            for ei, elem in enumerate(code):
                segments.append((True, dit if elem == "." else 3 * dit))
                if ei < len(code) - 1:
                    segments.append((False, dit))
            if li < len(word) - 1:
                segments.append((False, 3 * dit))
        if wi < len(words) - 1:
            segments.append((False, 7 * dit))
    return segments


if __name__ == "__main__":
    assert dit_ms(12) == 100.0  # 12 WPM -> 100ms dit, the classic reference speed

    e = encode_text("E", 20)
    dit = dit_ms(20)
    assert e == [(True, dit)]

    sos = encode_text("SOS", 20)
    on_segments = [d for on, d in sos if on]
    assert on_segments == [dit, dit, dit, 3 * dit, 3 * dit, 3 * dit, dit, dit, dit]

    two_words = encode_text("HI THERE", 20)
    assert (False, 7 * dit) in two_words  # inter-word gap present

    assert encode_text("", 20) == []
    assert encode_text("~", 20) == []  # unknown char skipped, no crash

    print("morse.py: ok")
