# voiceover.py
#
# Turns a script into a voiceover in the cloned voice, using the reference
# clip from my_voice.wav. Splits long text into sentence-boundary chunks
# before generating, since Chatterbox's per-call generation has a fixed
# step budget that full scripts can exceed — chunking avoids truncation
# or quality loss on longer text.

import re
import torch
import torchaudio as ta
from chatterbox.tts import ChatterboxTTS

MAX_CHUNK_CHARS = 250  # conservative — well under where longer-text issues tend to show up


def split_into_chunks(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split text into chunks at sentence boundaries, each under max_chars."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks, current = [], ""
    for sentence in sentences:
        if len(current) + len(sentence) + 1 <= max_chars:
            current = f"{current} {sentence}".strip()
        else:
            if current:
                chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def generate_voiceover(
    script_text: str,
    reference_path: str = "osho-voice.mp3",
    output_path: str = "voiceover.wav",
    device: str = "mps",
) -> str:
    """Generate a voiceover from script text, cloned in the voice from reference_path."""
    print("Loading Chatterbox model...")
    model = ChatterboxTTS.from_pretrained(device=device)

    chunks = split_into_chunks(script_text)
    print(f"Split script into {len(chunks)} chunk(s)")

    audio_segments = []
    for i, chunk in enumerate(chunks, 1):
        print(f"  [{i}/{len(chunks)}] {chunk[:60]}...")
        wav = model.generate(chunk, audio_prompt_path=reference_path)
        audio_segments.append(wav)

    full_audio = torch.cat(audio_segments, dim=-1)
    ta.save(output_path, full_audio, model.sr)
    print(f"Saved {output_path}")
    return output_path


if __name__ == "__main__":
    # Use an actual full-length script here, not a short test sentence —
    # the whole point is confirming chunking works on realistic length.
    script = (
        "Avoid janky animations and improve user experience with useTransition in React! "
        "When using useTransition, your component will render twice - once immediately, "
        "and again after the transition. This happens because React needs to reconcile "
        "the new state before applying it, ensuring a smooth update. Mastering "
        "useTransition will help you optimize your React app for better performance "
        "and user experience - check out our resources in the description below to learn more!"
    )
    generate_voiceover(script)