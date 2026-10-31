# Credits

Harkenn stands on the work of many open-source projects. Thank you to all of them.

## Speech and voice

| Project | Used for | Licence |
|---|---|---|
| [Whisper](https://github.com/openai/whisper) (OpenAI) | Speech recognition models | MIT |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (SYSTRAN) | Running Whisper on the CPU, and its converted models | MIT |
| [CTranslate2](https://github.com/OpenNMT/CTranslate2) (OpenNMT) | Inference engine behind faster-whisper | MIT |
| [Piper](https://github.com/OHF-Voice/piper1-gpl) (Open Home Foundation, originally Rhasspy) | Speech synthesis | GPL-3.0-or-later |
| [espeak-ng](https://github.com/espeak-ng/espeak-ng) | Phonemes for Piper (bundled with it) | GPL-3.0-or-later |
| [Piper voices](https://huggingface.co/rhasspy/piper-voices) | The voices. Downloaded when first used, not shipped with Harkenn; each voice has its own licence on its model card | per voice |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) (Microsoft) | Runs the Piper voices | MIT |
| [Wyoming protocol](https://github.com/OHF-Voice/wyoming) (Open Home Foundation) | Talking to external speech services; Harkenn has its own small client | protocol |
| [Hugging Face Hub](https://github.com/huggingface/huggingface_hub) | Downloading models | Apache-2.0 |
| [PyAV](https://github.com/PyAV-Org/PyAV) | Audio decoding for faster-whisper (bundles FFmpeg, LGPL) | BSD-3-Clause |

## Server

| Project | Used for | Licence |
|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) and [Starlette](https://github.com/Kludex/starlette) | Web framework | MIT, BSD-3-Clause |
| [Uvicorn](https://github.com/encode/uvicorn) | Web server | BSD-3-Clause |
| [Pydantic](https://github.com/pydantic/pydantic) | Settings and request validation | MIT |
| [Jinja](https://github.com/pallets/jinja) and [ItsDangerous](https://github.com/pallets/itsdangerous) (Pallets) | Admin pages, signed sessions | BSD-3-Clause |
| [python-multipart](https://github.com/Kludex/python-multipart) | Uploads | Apache-2.0 |
| [Pillow](https://github.com/python-pillow/Pillow) | Drawing and converting character animations | MIT-CMU |
| [NumPy](https://numpy.org) | Animation encoding, audio resampling | BSD-3-Clause |
| [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf) | Announcing the server on the network (mDNS) | LGPL-2.1-or-later |
| [SQLite](https://sqlite.org) | Database | public domain |

## Ideas and protocols

- The speaker's Wi-Fi setup, adoption code and over-the-air updates follow patterns from
  [ESPHome](https://esphome.io) and [Home Assistant](https://www.home-assistant.io); the voice pipeline follows the
  shape of Home Assistant's Assist, and can use the same Wyoming services.
- The hardware notes for the Spotpear ESP32-S3 speaker come from the board definition in
  [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) (MIT).

## Characters

The built-in characters (Kotodama, Obake, Neko and Robo) are drawn in code in `app/starter.py` and belong to this
project. Kotodama (言霊, "word spirit") was the project's first name.
