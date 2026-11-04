# Music, soundscapes and meditations

A speaker with a micro SD card plays music, soundscapes and meditations from it. The card is optional: a speaker
without one works exactly the same, and Harkenn hides the media features for it.

Everything plays from the card, on the speaker, so it keeps playing even if Wi-Fi drops. Harkenn only says what to
play.

## Asking for it

Say the wake word, then:

| Say | It does |
|---|---|
| "Play rain", "play some music", "play white noise for an hour" | Plays it (with a sleep timer, if you give one) |
| "Play the breathing meditation", "put on the campfire for 20 minutes" | |
| "Pause", "resume", "stop" | |
| "Next", "skip", "go back" | Moves through an album |
| "Repeat this one", "repeat the album", "stop repeating" | |
| "Shuffle" | Plays the album in a random order |
| "Stop in half an hour", "set a sleep timer for 45 minutes" | Fades out over 8 seconds, then stops |
| "What's playing?" | Says the track's name |

Harkenn catches these before they reach the assistant, the same way it catches broadcasts. They're quick, and they
work with any assistant, Hermes included. Something that isn't on the card ("play Despacito") goes on to the
assistant as usual. Names are matched loosely: "play rain" finds *City Rain*, "play some piano" finds
*Background*.

Music fades out while the speaker listens, talks or rings (a timer, an alarm, an agent), and fades back in after.
Pause, stop and skip fade briefly too, so nothing clicks.

AI agents can play and control media through MCP: `media_library`, `play_media` and `control_media`
([MCP.md](MCP.md)).

## The media library

The admin's **Media** section keeps music albums, soundscapes and meditations on the server, ready for the speakers'
cards. Music shows the albums as covers; each album has its own page with its track list. Soundscapes are tiles;
meditations a list. Anything can be played in the browser.

- **Albums**: create, rename and delete them; add tracks (several at once); move tracks up and down; give an album a
  cover (any picture, cropped square and saved as `cover.jpg` in its folder) or let Harkenn make one from its name.
- **Adding files**: any common audio format. Harkenn makes them card-friendly: music and meditations become mono
  MP3s at an even loudness; soundscapes become mono 32 kHz WAVs that loop with no seam (the end crossfades into the
  start).
- **Details**: each file can carry its name, who made it, its licence and a link (⋯ → Edit details).
- **Listen** to anything in the browser: a player docks at the bottom of the page.

The library is a plain folder, `HARKENN_MEDIA_DIR` (default `media` in the data folder), laid out exactly like a
card, so it can also be copied onto a card directly. It can be a volume of its own.

**The sample media** fills a new library once. At startup, if the library has no marker file (`.sample-media`),
Harkenn downloads the sample media, checks every file against its manifest, and only then writes the marker. After
that it never fills it again: not on restarts, not on updates, and samples you delete stay deleted. If the download
fails (no internet, say), it tries again at the next start. **Add the sample media again** on the Media page puts
back any of it you've removed, without touching your own files. `HARKENN_SAMPLE_MEDIA=false` turns it off.

## How each kind plays

- **Music** plays an album's tracks in order, by file name (so `01 ...`, `02 ...` sets the order), then starts over.
  "Repeat this one" loops a single track.
- **A soundscape** loops until it's stopped, changed, or its sleep timer runs out. A WAV file loops with no gap at
  all; an MP3 leaves a tiny one, because MP3 pads the ends of a file.
- **A meditation** plays once and stops.

## The card

The card must be **FAT32** (FAT16 works too). Cards up to 32 GB usually come that way. Bigger cards come as exFAT,
which the speaker can't read: the speaker's admin page then says *can't be read*, and **⋯ → Format the SD card…**
makes it FAT32. Formatting erases everything on the card. You can also format a card as FAT32 on a computer.

The slot can't tell when a card goes in or out, so the speaker looks at boot. After swapping a card, restart the
speaker, or use **⋯ → Look for the SD card again** on its admin page.

### Layout

Plain folders, so you can fill a card on any computer:

```
/Music/<album>/*.mp3, *.wav          one folder per album; cover.jpg is its artwork
/Soundscapes/*.wav, *.mp3            one file per soundscape
/Meditations/*.mp3, *.wav            one file per meditation
/Photos/<album>/*.jpg                photo albums (for the photo frame, coming)
```

A file's or folder's name is what you say and what the speaker shows, without the extension and any leading track
number: `03 Clair de lune.mp3` is *Clair de lune*. Hidden files (starting with `.`, like the `._` files a Mac leaves)
are skipped. Up to 64 albums or files are listed per folder.

### Files

- **MP3**, any bit rate, or **WAV** (16-bit PCM), at any sample rate, mono or stereo. The speaker has one driver,
  so stereo is mixed to mono; mono files are half the size.
- Sound is filtered below 11 kHz, which is about all a speaker this size can play, and keeps music away from the
  tones speakers use to decide which one heard the wake word.
- **For soundscapes, use WAV**, for loops without a gap, and make the end flow into the start (a short crossfade).

## The sample media

[harkenn-project/samplemedia](https://github.com/harkenn-project/samplemedia) has, all free to share:

- **Background**: ten piano pieces (Satie, Chopin, Debussy, Schubert, Beethoven), recordings that are themselves
  public domain or CC0.
- **Soundscapes**: rain, a thunderstorm, a stream, wind, a campfire, birdsong, night sounds and more, CC0 field
  recordings from [Freesound](https://freesound.org) made into seamless loops, plus white noise.
- **Meditations**: coming. Good guided meditations we're allowed to share are hard to find.

Each file's source and licence is listed there, and shows beside it on the Media page. Please only put things on a card you're allowed
to: "no copyright" on a website usually means "free to use in your own project", not "free to hand out".

## What the speaker reports

The speaker tells Harkenn what's on its card (`card`) and what it's playing (`media_state`), and Harkenn sends it
`media` commands. The speaker's admin page shows the card: its free space, and how many albums, soundscapes and
meditations are on it. The messages are in [PROTOCOL.md](PROTOCOL.md#10-the-micro-sd-card-firmware-014-and-later).

## Coming next

- A player screen on the speaker, with the album art and play, pause, next and repeat buttons.
- Playing and controlling from the admin page.
- Copying albums from the library to a speaker's card, from its admin page.
- A photo frame: the speaker's sleep screen showing photo albums from the card.
