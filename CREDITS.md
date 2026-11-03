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
| [Piper voices](https://huggingface.co/rhasspy/piper-voices) | The voices. Downloaded when first used, not shipped with Harkenn; each voice has its own licence on its model card. The default, [LJ Speech](https://keithito.com/LJ-Speech-Dataset/), is public domain; Lessac's dataset allows research use only | per voice |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) (Microsoft) | Runs the Piper voices | MIT |
| [Wyoming protocol](https://github.com/OHF-Voice/wyoming) (Open Home Foundation) | Talking to external speech services; Harkenn has its own small client | protocol |
| [Hugging Face Hub](https://github.com/huggingface/huggingface_hub) | Downloading models | Apache-2.0 |
| [PyAV](https://github.com/PyAV-Org/PyAV) | Audio decoding for faster-whisper, sound uploads and browser recordings (bundles FFmpeg, LGPL) | BSD-3-Clause |
| [Silero VAD](https://github.com/snakers4/silero-vad) | Hearing when someone has finished talking (the model bundled with faster-whisper) | MIT |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) (k2-fsa) | Computing voiceprints | Apache-2.0 |
| [WeSpeaker](https://github.com/wenet-e2e/wespeaker) ResNet34 model | Voiceprints; trained on [VoxCeleb](https://www.robots.ox.ac.uk/~vgg/data/voxceleb/) (CC BY 4.0). Downloaded when first used | Apache-2.0 |
| [micro-wake-word-models](https://github.com/esphome/micro-wake-word-models) (ESPHome / Open Home Foundation; models by Kevin Ahrendt, trained with [microWakeWord](https://github.com/kahrendt/microWakeWord)) | The wake word models (Okay Nabu, Hey Jarvis, Hey Mycroft, Alexa), bundled in the image and run on the speakers | Apache-2.0 |

## Server

| Project | Used for | Licence |
|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) and [Starlette](https://github.com/Kludex/starlette) | Web framework | MIT, BSD-3-Clause |
| [Uvicorn](https://github.com/encode/uvicorn) | Web server | BSD-3-Clause |
| [Pydantic](https://github.com/pydantic/pydantic) | Settings and request validation | MIT |
| [Jinja](https://github.com/pallets/jinja) and [ItsDangerous](https://github.com/pallets/itsdangerous) (Pallets) | Admin pages, signed sessions | BSD-3-Clause |
| [python-multipart](https://github.com/Kludex/python-multipart) | Uploads | Apache-2.0 |
| [Pillow](https://github.com/python-pillow/Pillow) | Drawing and converting character animations and screens | MIT-CMU |
| [Aileron](https://www.dotcolon.net/font/aileron/) (Sora Sagano, bundled with Pillow) | The lettering on the assistant's buttons and text | CC0-1.0 |
| [CairoSVG](https://github.com/Kozea/CairoSVG) (Kozea) and [cairo](https://www.cairographics.org/) | Drawing the assistant's SVG screens | LGPL-3.0 / LGPL-2.1 |
| [DejaVu fonts](https://dejavu-fonts.github.io) and [Fontconfig](https://www.freedesktop.org/wiki/Software/fontconfig/) | Text in SVG screens (installed in the image from Debian) | Bitstream Vera licence (free) / MIT |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (Model Context Protocol) | The MCP server agents use | MIT |
| [NumPy](https://numpy.org) | Animation encoding, audio resampling | BSD-3-Clause |
| [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf) | Announcing the server on the network (mDNS) | LGPL-2.1-or-later |
| [pyca/cryptography](https://github.com/pyca/cryptography) | Keeping the speaker passwords encrypted; AES-GCM for the relay | Apache-2.0 or BSD-3-Clause |
| [websockets](https://github.com/python-websockets/websockets) (Aymeric Augustin) | Meeting speakers at the relay | BSD-3-Clause |
| [HTTPX](https://github.com/encode/httpx) (Encode) | Serving speakers' downloads through the relay | BSD-3-Clause |
| [SQLite](https://sqlite.org) | Database | public domain |

## Ideas and protocols

- The speakers appear in Home Assistant through its [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
  convention; Harkenn has its own small MQTT 3.1.1 client ([MQTT](https://mqtt.org) is an OASIS standard).
- Agents reach the speakers over the [Model Context Protocol](https://modelcontextprotocol.io).
- The speaker's Wi-Fi setup, adoption code and over-the-air updates follow patterns from
  [ESPHome](https://esphome.io) and [Home Assistant](https://www.home-assistant.io); the voice pipeline follows the
  shape of Home Assistant's Assist, and can use the same Wyoming services.
- The hardware notes for the Spotpear ESP32-S3 speaker come from the board definition in
  [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) (MIT).

## Pip

Pip, Master Magic's AI apprentice, is the name we give the AI assistant that co-writes Harkenn. Pip's portrait is drawn in code by `tools/draw_pip.py`.

## Characters

The built-in characters (Kotodama, Obake, Neko and Robo) are drawn in code in `app/starter.py` and belong to this
project. Kotodama (言霊, "word spirit") was the project's first name.
