# voice_test.py
#
# Minimal smoke test — confirms Chatterbox installs and runs on your
# hardware before we wire it into anything. Weights (~a few hundred MB)
# download automatically from Hugging Face on first run.

import torchaudio as ta
from chatterbox.tts import ChatterboxTTS

print("Loading model (first run will also download weights)...")
try:
    model = ChatterboxTTS.from_pretrained(device="mps")
    print("Using Apple Silicon MPS.")
except Exception as e:
    print(f"MPS failed ({e}), falling back to CPU...")
    model = ChatterboxTTS.from_pretrained(device="cpu")

text = "This is a test of Chatterbox running entirely on my own machine."
wav = model.generate(text)
ta.save("test_output.wav", wav, model.sr)
print("Saved test_output.wav")