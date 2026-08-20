"""Воркер запускается отдельным Python 3.11 из tts-venv и держит модель в памяти."""
import argparse
import json
from pathlib import Path

import torch  # type: ignore[import-not-found]
import torchaudio  # type: ignore[import-not-found]
from chatterbox.mtl_tts import (  # type: ignore[import-not-found]
    ChatterboxMultilingualTTS,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    device = job.get("device", "cuda")
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("Chatterbox запросил CUDA, но PyTorch не видит видеокарту")

    model = ChatterboxMultilingualTTS.from_pretrained(device=device, t3_model="v3")
    prompt = job.get("voice_reference") or None
    pieces = []
    sample_rate = model.sr
    chunk_dir = Path(args.output).with_suffix("")
    chunk_dir.mkdir(parents=True, exist_ok=True)

    for index, text in enumerate(job["chunks"]):
        kwargs = {
            "language_id": "ru",
            "cfg_weight": float(job.get("cfg_weight", 0.0)),
            "exaggeration": float(job.get("exaggeration", 0.35)),
            "temperature": float(job.get("temperature", 0.55)),
        }
        if prompt:
            kwargs["audio_prompt_path"] = prompt
        with torch.inference_mode():
            wav = model.generate(text, **kwargs).cpu()
        if wav.ndim == 1:
            wav = wav.unsqueeze(0)
        chunk_path = chunk_dir / f"chunk_{index:03d}.wav"
        torchaudio.save(str(chunk_path), wav, sample_rate)
        pieces.append(wav)
        pieces.append(torch.zeros((1, int(sample_rate * 0.22))))
        if device == "cuda":
            torch.cuda.empty_cache()

    combined = torch.cat(pieces[:-1], dim=1)
    torchaudio.save(args.output, combined, sample_rate)
    print(json.dumps({"sample_rate": sample_rate, "chunks": len(job["chunks"]), "output": args.output}))


if __name__ == "__main__":
    main()
