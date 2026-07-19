# clone-test.py
import torchaudio as ta
from chatterbox.tts import ChatterboxTTS

model = ChatterboxTTS.from_pretrained(device="mps")

wav = model.generate(
    "Testing this in my own cloned voice now.",
    audio_prompt_path="aditya-voice.m4a",
)
ta.save("cloned_test.wav", wav, model.sr)
print("Saved cloned_test.wav")