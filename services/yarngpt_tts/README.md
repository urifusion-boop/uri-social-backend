# YarnGPT speech service

Speaks a line in a Nigerian-accented voice and returns a 24 kHz WAV. The video pipeline calls
it for talking videos with a Nigerian voice (`app/agents/social_media_manager/services/yarngpt_tts_service.py`),
so the audio reaching the lip-sync model is YarnGPT's own and not a clone of it.

YarnGPT is open ([code, MIT](https://github.com/saheedniyi02/yarngpt),
[weights, Apache-2.0](https://huggingface.co/saheedniyi/YarnGPT2)) and has no hosted API, which is
why this exists. Nothing in the backend loads the model; the backend only makes HTTP calls.

## Build and run

```
docker build -f services/yarngpt_tts/Dockerfile -t yarngpt-tts .
docker run -p 8080:8080 -e TTS_TOKEN=change-me yarngpt-tts
```

The image bakes in the model (about 2 GB), so a restart never waits on the internet. It is
CPU-only. On a GPU host, replace the `torch` line in the Dockerfile with the CUDA wheels.

- `GET /healthz` returns `{"ready": true}` once the model has loaded (about a minute on first start).
- `POST /synthesize` with `{"text": "...", "speaker": "idera", "lang": "english"}` and the header
  `Authorization: Bearer <TTS_TOKEN>` returns `audio/wav`.

## Sizing

One synthesis at a time per process; a second request waits. A 15-word line is about 5 seconds of
speech. Time it on the host you choose (the first call is slower) and set the replica count from
that: a talking video of 12 scenes makes 12 calls in a row.

## Connect the backend

Set on the backend (staging first):

```
YARNGPT_TTS_URL=https://<where the service runs>
YARNGPT_TTS_TOKEN=<same value as TTS_TOKEN>
```

Unset `YARNGPT_TTS_URL` and nothing changes: Nigerian voices use the older cloning path. If a
call fails, Idera and Jude fall back to cloning; the other voices fall back to the outcome's
paired model.

## What the text cleanup does

YarnGPT deletes every character that is not a letter and spells digits as words, so
`normalize.py` first turns `₦31,000` into `31000 naira`, `&` into `and`, `50%` into `50 percent`,
`AI` into `A I`, and drops apostrophes. It also means punctuation gives no pause: write lines as
plain sentences.

## Voices

English: idera, chinenye, jude, emma, umar, joke, zainab, osagie, remi, tayo (plus onye,
regina, azeez, saheed, which exist in the repository but are not offered in the app).
Yoruba, Igbo and Hausa voices exist in the model; they are switched off here (`lang` must be
`english`) because the script writer produces English.

## Known limits (from the model card)

It may not capture the full range of Nigerian accents, was trained partly on auto-generated
text, and is described as experimental. Listen before putting a voice in front of customers.
