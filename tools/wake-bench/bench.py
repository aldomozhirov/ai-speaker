#!/usr/bin/env python3
"""Score wake-word engines on the speaker's own captures (throwaway comparison).

Runs every candidate over each 2-channel capture, one channel at a time, and
writes what each engine made of it to results.json. Nothing here touches the
live services; run it niced:

    OMP_NUM_THREADS=1 nice -n 19 venv/bin/python bench.py
    venv/bin/python report.py
"""

from __future__ import annotations

import glob
import json
import os
import sys
import tempfile
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.expanduser("~/.local/share/ai-speaker")
RESULTS = os.path.join(HERE, "results.json")
KWS_DIR = os.path.join(HERE, "models", "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01")
VOSK_DIR = os.path.join(DATA, "vosk-model-small-en-us-0.15")
PORCUPINE_SENSITIVITIES = (0.3, 0.5, 0.7, 0.9, 1.0)
# (boost, threshold): sherpa-onnx's two knobs, from its default to very eager
KWS_SETTINGS = ((1.0, 0.25), (1.5, 0.15), (2.0, 0.10), (3.0, 0.05))


def read_channels(path: str) -> list[np.ndarray]:
    with wave.open(path) as wav:
        assert wav.getframerate() == 16000 and wav.getsampwidth() == 2, path
        audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
        audio = audio.reshape(-1, wav.getnchannels())
    return [np.ascontiguousarray(audio[:, c]) for c in range(audio.shape[1])]


class OpenWakeWord:
    name = "oww"

    def __init__(self) -> None:
        import openwakeword
        from openwakeword.model import Model

        path = next(p for p in openwakeword.get_pretrained_model_paths() if "hey_jarvis" in p)
        self.model = Model(wakeword_model_paths=[path])
        self.key = next(iter(self.model.models))

    def score(self, pcm: np.ndarray) -> dict:
        self.model.reset()
        peak = 0.0
        for start in range(0, len(pcm) - 1280 + 1, 1280):
            peak = max(peak, float(self.model.predict(pcm[start:start + 1280])[self.key]))
        return {"peak": round(peak, 4)}


class Porcupine:
    name = "porcupine"

    def __init__(self) -> None:
        # pvporcupine 1.9.5 refuses to import on a Pi 5 (unknown CPU part), so
        # its engine class is loaded directly and given the Pi 4 library.
        import importlib.util

        spec = importlib.util.find_spec("pvporcupine")
        base = spec.submodule_search_locations[0]
        engine_spec = importlib.util.spec_from_file_location("pv_engine", os.path.join(base, "porcupine.py"))
        engine = importlib.util.module_from_spec(engine_spec)
        engine_spec.loader.exec_module(engine)
        self.handles = {
            s: engine.Porcupine(
                library_path=os.path.join(base, "lib/raspberry-pi/cortex-a72-aarch64/libpv_porcupine.so"),
                model_path=os.path.join(base, "lib/common/porcupine_params.pv"),
                keyword_paths=[os.path.join(base, "resources/keyword_files/raspberry-pi/jarvis_raspberry-pi.ppn")],
                sensitivities=[s],
            )
            for s in PORCUPINE_SENSITIVITIES
        }

    def score(self, pcm: np.ndarray) -> dict:
        # Porcupine keeps state between files; a second of silence clears it.
        samples = np.concatenate([pcm, np.zeros(16000, dtype=np.int16)])
        hits = {}
        for sensitivity, handle in self.handles.items():
            n = handle.frame_length
            count = sum(
                handle.process(samples[start:start + n].tolist()) >= 0
                for start in range(0, len(samples) - n + 1, n)
            )
            hits[str(sensitivity)] = count
        return {"hits": hits}


class MicroWakeWord:
    name = "micro"

    def __init__(self) -> None:
        from pymicro_wakeword import MicroWakeWord as Engine, Model

        self.engine_class, self.model = Engine, Model.HEY_JARVIS

    def score(self, pcm: np.ndarray) -> dict:
        from pymicro_wakeword import MicroWakeWordFeatures

        engine = self.engine_class.from_builtin(self.model)  # fresh state per file
        features = MicroWakeWordFeatures()
        peak = 0.0
        data = pcm.tobytes()
        for start in range(0, len(data), 320):  # 10 ms
            for frame in features.process_streaming(data[start:start + 320]):
                prob = engine.process_streaming_prob(frame)
                if prob:
                    peak = max(peak, float(prob))
        return {"peak": round(peak, 4)}


class SherpaKws:
    name = "sherpa"

    def __init__(self) -> None:
        import sherpa_onnx

        self.spotters = {}
        for boost, threshold in KWS_SETTINGS:
            keywords = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
            keywords.write("▁HE Y ▁JA R VI S @HEY_JARVIS\n▁JA R VI S @JARVIS\n")
            keywords.close()
            self.spotters[f"{boost}/{threshold}"] = sherpa_onnx.KeywordSpotter(
                tokens=os.path.join(KWS_DIR, "tokens.txt"),
                encoder=os.path.join(KWS_DIR, "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
                decoder=os.path.join(KWS_DIR, "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
                joiner=os.path.join(KWS_DIR, "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"),
                keywords_file=keywords.name,
                num_threads=1,
                keywords_score=boost,
                keywords_threshold=threshold,
            )

    def score(self, pcm: np.ndarray) -> dict:
        samples = np.concatenate([pcm, np.zeros(8000, dtype=np.int16)]).astype(np.float32) / 32768.0
        hits = {}
        for label, spotter in self.spotters.items():
            stream = spotter.create_stream()
            found = []
            for start in range(0, len(samples), 1600):
                stream.accept_waveform(16000, samples[start:start + 1600])
                while spotter.is_ready(stream):
                    spotter.decode_stream(stream)
                    keyword = spotter.get_result(stream)
                    if keyword:
                        found.append(keyword)
                        spotter.reset_stream(stream)
            hits[label] = found
        return {"hits": hits}


class VoskGrammar:
    """A small recogniser allowed to hear only the wake phrase or 'something else'."""

    name = "vosk"

    def __init__(self) -> None:
        import vosk

        vosk.SetLogLevel(-1)
        self.vosk = vosk
        self.model = vosk.Model(VOSK_DIR)
        self.grammar = json.dumps(["hey jarvis", "jarvis", "[unk]"])

    def score(self, pcm: np.ndarray) -> dict:
        recogniser = self.vosk.KaldiRecognizer(self.model, 16000, self.grammar)
        recogniser.SetWords(True)
        words = []
        data = pcm.tobytes()
        for start in range(0, len(data), 8000):
            if recogniser.AcceptWaveform(data[start:start + 8000]):
                words += json.loads(recogniser.Result()).get("result", [])
        words += json.loads(recogniser.FinalResult()).get("result", [])
        confidences = [round(w["conf"], 3) for w in words if w["word"] == "jarvis"]
        return {"jarvis_conf": confidences}


def capture_files() -> list[str]:
    files = []
    for pattern in ("wake-verifier/positive", "wake-verifier/negative",
                    "wake-verifier/unlabelled-ambiguous", "wake-captures"):
        files += sorted(glob.glob(os.path.join(DATA, pattern, "*.wav")))
    return files


def main() -> int:
    only = sys.argv[1:]
    results = {}
    if os.path.exists(RESULTS):
        with open(RESULTS) as handle:
            results = json.load(handle)
    engines = [OpenWakeWord(), Porcupine(), MicroWakeWord(), SherpaKws(), VoskGrammar()]
    files = only or capture_files()
    for index, path in enumerate(files):
        entry = results.setdefault(path, {})
        channels = None
        for engine in engines:
            if engine.name in entry:
                continue
            channels = channels or read_channels(path)
            entry[engine.name] = [engine.score(pcm) for pcm in channels]
        if channels is not None and (index % 20 == 0 or only):
            with open(RESULTS + ".tmp", "w") as handle:
                json.dump(results, handle)
            os.replace(RESULTS + ".tmp", RESULTS)
            print(f"{index + 1}/{len(files)} {os.path.basename(path)} {json.dumps(entry)[:400]}", flush=True)
    with open(RESULTS + ".tmp", "w") as handle:
        json.dump(results, handle)
    os.replace(RESULTS + ".tmp", RESULTS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
