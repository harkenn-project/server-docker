# Remote speakers: the relay

A speaker away from home (at a friend's, on holiday) usually can't reach its Harkenn server, which lives on the home
network. If it's set up for remote connection, it goes through a relay instead: a small public service that both the
speaker and Harkenn connect out to. The relay pairs them and passes their frames along. It can't read or change
them: they're encrypted with a key only the speaker and Harkenn have.

```
speaker ──wss──▶ relay ◀──ws(s)── Harkenn
        /relay/speaker/<id>   /relay/harkenn/<id>
```

## Setting it up

* The relay is the `harkenn-project/flash` service (it's also the web flasher). Point Harkenn at it with
  `HARKENN_RELAY_URL` (what speakers use, e.g. `wss://flash.mylife.ninja`) and, if Harkenn reaches it another way
  (inside a cluster), `HARKENN_RELAY_SERVER_URL`.
* Turn on **Remote connection** for a speaker on its page. Its next config carries
  `"remote": {"url", "id", "key"}`: the relay's address, a random 128-bit id (32 hex digits) and a random 256-bit
  key (64 hex digits). Turning it off sends `"remote": null` and the speaker forgets them.
* The speaker reports the id it holds as `remote` in its status. From then on, whenever that speaker is offline,
  Harkenn waits for it at the relay (`/relay/harkenn/<id>`), checking every 15 s. It stops once the speaker is back
  directly.

## When the speaker uses it

When it has Wi-Fi but can't find Harkenn for 2 minutes, it connects to `<url>/relay/speaker/<id>`. While on the
relay it shows a remote icon, and tries Harkenn directly every 5 minutes; when that works it leaves the relay. The
admin page shows the speaker as online *remotely*, with a warning.

## Protocol

The relay sends each side the text frame `{"type":"paired"}` once both are there. From then on everything is
binary, and the relay passes binary frames untouched (text frames from the sides are dropped). When either side
leaves, the relay closes the other (code 4001); both start over.

**Handshake.** `K` is the key; `HMAC` is HMAC-SHA256; `hn` and `sn` are 16 random bytes from Harkenn and the speaker.

```
Harkenn → speaker   0x01 hn
speaker → Harkenn   0x02 sn HMAC(K, "harkenn-relay speaker" hn sn)[0:16]
Harkenn → speaker   0x03    HMAC(K, "harkenn-relay harkenn" hn sn)[0:16]
```

Each side checks the other's proof and gives up on a wrong one. The session keys are
`Ks = HMAC(K, "harkenn-relay s2h" hn sn)` (speaker to Harkenn) and `Kh = HMAC(K, "harkenn-relay h2s" hn sn)`.

**Data.** Every frame after that is `0x10 counter(8, big-endian) ciphertext`, where the ciphertext is AES-256-GCM
(16-byte tag at the end, no additional data) of `kind payload` with nonce `00000000 counter`, under the sender's
session key. Counters start at 1 for each direction and must go up: a receiver drops the session on a repeated,
older, or forged frame. `kind` is:

* `T`: a text frame of the usual device protocol (PROTOCOL.md), JSON.
* `B`: a binary frame of the usual device protocol (microphone audio, speech, a screen's picture).
* `F`: part of a fetched file: the fetch id (4 bytes, big-endian), then data.

**Inside.** The speaker's first frame is `T {"type":"hello","token":"<its device token>"}`; Harkenn checks it like
the `Authorization` header of a direct connection (a wrong one closes with 4401). Then it's the device WebSocket
protocol, exactly as if connected directly, plus:

* `fetch {id, url}` (speaker → Harkenn): a file the speaker would download over HTTP: an animation, a sound clip,
  the wake word model, a screen's picture or a firmware image (paths under `/assets/`, `/api/v1/screens/`,
  `/api/v1/characters/`, `/api/v1/device/firmware/`). The URL's host is ignored.
* `fetch_result {id, status, size}` (Harkenn → speaker), then `size` bytes in `F` frames of at most 16 KB. Anything
  but status 200 has no data. Files are at most 4 MB.

## What the relay sees

Which speaker ids connect, from which addresses, when, and how much they send. Not the token, the conversation,
the speaker's name or anything else in the frames. Someone who learns an id can only disturb a pairing (connect in
its place); without the key, the handshake fails.
