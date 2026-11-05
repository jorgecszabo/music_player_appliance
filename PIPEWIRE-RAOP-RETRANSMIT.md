# PipeWire RAOP sink: answer retransmit requests

Plan for an upstream PipeWire merge request: make `module-raop-sink` answer
AirPlay 1 (RAOP) retransmit requests. Today it discards them, so every UDP packet
lost on the way to the receiver is gone for good. Code references are to PipeWire
**1.6.11** (Fedora 44) and shairport-sync **5.5.1** (the appliance).

## Why

Investigated on 2026-11-05: AirPlay from the Fedora desktop (PipeWire 1.6.11) to
the Panda (shairport-sync 5.5.1, r92su USB Wi-Fi).

- The steady faint crackle was shairport's `interpolation = "basic"` sync
  corrections. Fixed on the receiver (`vernier`, commit bc0a089). Unrelated to
  this plan.
- What's left is an occasional click, and each one is one RTP packet lost on the
  Wi-Fi link:
  - 0.01-0.13 % of audio packets lost, varying by run.
  - Before each loss, the r92su reorder buffer holds the following frames for its
    100 ms timeout.
  - shairport asks for the packet again ("Resend Reqs" counts up), and no answer
    ever comes.
  - shairport then skips the missing 352 frames: no silence is inserted, the
    stream just jumps ahead 8 ms. That's a click in music, followed by a few
    seconds of maximum-rate sync correction.
  - Measured with a 1102.5 Hz tone (8.8 cycles per packet, so a skip is a 72°
    phase jump). The seam lined up with the lost packet's media time to within
    1 ms.
- Over Ethernet: 0 losses and 0 resend requests in 3 minutes. Shairport's
  minimum buffer stayed steady at 124-125 packets; on Wi-Fi it dipped to
  104-113.
- shairport buffers about 1-2 s of audio (`raop.latency.ms`, default 1500 ms in
  the sink), so a resent packet almost always arrives in time. The protocol's
  design expects the sender to answer.

Answering retransmits fixes this for every PipeWire user streaming to a
Wi-Fi AirPlay receiver, not just the appliance.

## The protocol (AirPlay 1 / RAOP over UDP)

There's no published spec. These details are from shairport-sync's
implementation, which works with Apple senders.

**Request: receiver → sender's control port.** 8 bytes, shairport `rtp.c:1183`
`rtp_request_resend()`:

| Offset | Size | Value |
|---|---|---|
| 0 | 1 | `0x80` (RTP v2) |
| 1 | 1 | `0xd5` (payload type `0x55` with the marker bit set) |
| 2 | 2 | receiver's own sequence number (shairport sends 1); ignore it |
| 4 | 2 | first missing audio sequence number, big-endian |
| 6 | 2 | number of missing packets, big-endian |

**Response: sender → receiver's control port.** Parsed by shairport `rtp.c:550`:

| Offset | Size | Value |
|---|---|---|
| 0 | 1 | `0x80` |
| 1 | 1 | `0xd6` (payload type `0x56` with the marker bit set) |
| 2 | 2 | not read by shairport; send the resent packet's sequence number |
| 4 | 12 | the original RTP header, byte for byte (seq at +2, timestamp at +4) |
| 16 | n | the original payload, encrypted exactly as it was on the audio port |

shairport takes the sequence number and timestamp from the embedded header, then
hands the payload to the same decrypt/decode path as normal audio
(`player_put_packet`). It ignores responses with fewer than 16 payload bytes. One
response packet per missing sequence number.

The TCP transport (`raop.transport = "tcp"`) doesn't need any of this.

## The current code

All in `src/modules/module-raop-sink.c` unless stated:

- **`on_control_source_io()` (around line 666).** Reads 8-byte control packets,
  checks the `0x80` version byte, and splits `packet[1]` into `seq` and `num`.
  For `0xd5` it only logs at debug level:
  ```c
  case 0xd5:
      pw_log_debug("retransmit request seq:%u num:%u", seq, num);
      /* retransmit request */
      break;
  ```
  The request is already parsed. Only the response is missing.
- **`stream_send_packet()` (around line 458).** This is the `send_packet` callback
  of the RTP stream. It builds one wire packet:
  - the 12-byte RTP header from `iov[0]`,
  - `write_codec_pcm()` into a stack buffer `out`,
  - AES encryption if `CRYPTO_RSA`,
  - then `sendmsg()` on `server_fd`.

  Nothing is kept after sending.
- **`aes_encrypt()` (line 287).** Re-inits AES-128-CBC with the session key and
  the same IV on every packet. Because of that, a stored encrypted payload can be
  resent unchanged, and there's no cipher state to replay.
- **Sockets.** `control_fd` is a UDP socket connected to the receiver's control
  port (`connect_socket()` in `rtsp_setup_reply()`, line 1028), so a plain
  `send()` reaches the right place.
- **Threads.**
  - `send_packet` runs on the RTP stream's **data loop**: from the stream's
    process callback, or from the flush timer `on_flush_timeout` on
    `impl->data_loop` (`module-rtp/stream.c:647-648`, `module-rtp/audio.c:564`).
  - `on_control_source_io()` runs on the **main loop**
    (`impl->loop = pw_context_get_main_loop()`, line 1797).

  Any shared retransmit buffer therefore needs either synchronisation or one
  thread owning it.
- **Session start.** `rtsp_do_record()` (line 907) announces the starting `seq`
  and `rtptime` from `rtp_stream_get_seq()`/`rtp_stream_get_time()`. A new
  RECORD, a FLUSH or a reconnect must invalidate stored packets.

## Proposed design

### 1. History of sent wire packets in the RAOP sink

- **What to store.** Store each packet exactly as it went on the wire (RTP header
  plus encrypted payload), in a fixed array of slots indexed by
  `seq % N_SLOTS`. Each slot holds:
  - the 16-bit sequence number it contains,
  - a valid flag,
  - the length,
  - `12 + 8 + mtu` bytes of data.
- **Why the wire packet, not audio from the stream's ring buffer.** Regenerating
  from audio would mean reaching into `module-rtp/stream.c`'s ring and redoing
  the codec and encryption. The ring may also have been overwritten already. A
  sink-local copy keeps the change inside one file.
- **Sizing.** The history must cover the receiver's buffer: `raop.latency.ms`
  (default 1500 ms) plus some margin.
  - At 352 frames per packet, 1.5 s is about 188 packets, so **256 slots**
    (about 2 s) is the natural power of two.
  - Memory: about 256 × 1.5 KB = 380 KB per RAOP sink.
  - Better: derive N from `impl->latency` and round up to a power of two. A user
    who raises `raop.latency.ms` then still gets full coverage.
- **When to fill it.** In `stream_send_packet()`, after encryption, copy into the
  slot. UDP only; skip for `PROTO_TCP`.
- **When to invalidate it.** Clear the valid flags at RECORD, FLUSH, teardown and
  `connection_cleanup()`. After that, a request for an old sequence number just
  misses.

### 2. Answer the request

In the `0xd5` case:

- **Cap the count.** Clamp `num` to `N_SLOTS`. A malicious or buggy receiver
  shouldn't be able to make us send 65,535 packets.
- **Find each packet.** For `i` in `0..num-1`: `s = (seq + i) & 0xffff`. Look up
  slot `s % N_SLOTS`. If it's valid and its stored seq equals `s`, send
  `{0x80, 0xd6, htons(s)} + stored bytes` with `send()` on `control_fd`.
  Otherwise count a miss.
- **Wrap-around.** The sequence number is uint16, so handle wrap explicitly. No
  signed compares.
- **Logging.** One `pw_log_debug` per request (seq, num, sent, missed). No
  per-packet logs in normal operation.
- **Repeated requests.** shairport resends a request after a while if nothing
  arrives. Answering each request is fine (it's idempotent), so no dedup logic is
  needed. It's worth checking whether a rate limit is wanted.

### 3. Threading (the part reviewers will look at hardest)

Writer: data loop (realtime). Reader: main loop. Options:

- **A. Handle the request on the data loop.** Use `pw_loop_invoke()` from the
  control handler, so the data-loop thread owns the history and sends the
  answer.
  - No locks.
  - The raop sink doesn't currently hold the data loop pointer, so it needs a
    small accessor on `rtp_stream`, or to acquire the same loop the stream uses.
  - Sending a few UDP packets from the RT thread is what it already does for
    audio.
- **B. Lock-free per-slot seqlock.**
  - The writer bumps a per-slot atomic counter to odd, copies, then bumps it to
    even.
  - The reader copies into a local buffer, then rechecks the counter (even and
    unchanged) and the stored seq. If either changed, it treats the slot as a
    miss.
  - Self-contained and RT-safe. More subtle code (memory ordering), which
    reviewers will want to be obviously right.
- **C. A mutex.** Simple, but it puts a lock in the RT send path. Likely to be
  rejected.

Recommendation: **A**. It matches PipeWire's usual pattern (keep RT state on the
RT thread, invoke into it), and the code stays obviously correct. Ask on the MR
or in an issue first which accessor the maintainers prefer for the data loop.

### 4. Out of scope (don't mix into the MR)

- No retransmit for the TCP transport.
- No changes to `module-rtp`'s plain RTP sender. Retransmission in plain RTP is
  RTCP NACK, a different mechanism.
- Don't change `raop.latency.ms` defaults.
- No changes to sync/timing packets.

## Testing plan

1. **Build PipeWire from git at the 1.6.11 tag** (then rebase to `master` for the
   MR): `meson setup builddir && meson compile -C builddir`. Run it uninstalled
   (`./pw-uninstalled.sh` in the tree), or stop the user services and run the
   built daemons. The system PipeWire stays untouched.
2. **Deterministic loss on the receiver.** Use shairport-sync's built-in fault
   injection (`scripts/shairport-sync.conf`, `diagnostics` section):
   ```
   diagnostics = {
       statistics = "yes";
       drop_this_fraction_of_audio_packets = 0.01;
   };
   ```
   `rtp.c:297` drops incoming audio packets at that rate. (`rtp.c:575` also drops
   control-port packets at random, so some responses are lost too, which is a
   realistic second test.) Check:
   - **Without the patch:** "Resend Reqs" climbs, and you get a click and a sync
     jump per drop.
   - **With the patch:** "Missing" stays 0, there are no 352-frame seams in a
     tone recording, and "Resend Reqs" and the drops stay roughly equal.
3. **Audio-level check.** Use the tone method from this investigation:
   1. Play 1102.5 Hz at −12 dBFS from Fedora.
   2. `pw-record --target airplay` on the receiver.
   3. Run a 2-sample sine predictor and count seams; a 352-frame skip shows as a
      72° phase jump.

   The scripts (`sinan.py`, `hp2.py`, `sniff.py`) were scratch files and are
   gone. Write cleaner versions if needed; they're simple.
4. **Real Wi-Fi.** Play to the Panda over r92su for 10+ minutes. Expect resend
   requests and zero seams.
5. **Regression checks.**
   - `raop.transport = "tcp"` still works.
   - `raop.encryption.type = "none"`, `"RSA"` and `"auth_setup"`.
   - Reconnect and pause/resume: no stale packets resent after a new RECORD.
   - Sequence-number wrap: start near 65535 by streaming long enough, or
     temporarily seed `seq` in a debug build.
   - Valgrind/ASan on the module for the copy paths.
6. **Other receivers if available** (an AirPort Express, or another RAOP
   implementation) to confirm the response format isn't shairport-specific.

## Upstream process

- **Before coding.** Search PipeWire's GitLab issues and MRs for "raop
  retransmit" / "resend". Someone may have started this, or the maintainers may
  have opinions on the threading. If there's nothing, open an issue with the
  evidence above (short version) and the proposed design, and ask about option A.
- **The MR.**
  - One focused commit, prefixed `module-raop-sink:`.
  - Follow the file's existing style (tabs, `pw_log_*`, `spa_` helpers).
  - The commit message explains the protocol bytes, because there's no spec to
    link to.
  - Include the test method (shairport's drop fraction) in the MR description.
- **Fedora until it lands.** Run a local or COPR build of PipeWire with the
  patch. The appliance needs no change, since this is all sender side.

## Related: already fixed upstream

PipeWire 1.6.8 (Alpine at the time) clicked every 2^20 frames: when a packet
straddled the RTP ring-buffer wrap, `stream_send_packet` encoded only `iov[1]` and
dropped `iov[2]`. 1.6.11 passes all the iovs to `write_codec_pcm()`. See
UNATTENDED.md task 1.

## Open questions

- Should the history size follow `raop.latency.ms` automatically, or be a module
  option (`raop.retransmit.packets`)? Automatic is simpler for users.
- Is a rate limit on answers wanted, against a misbehaving receiver asking for
  the same range in a loop?
- AirPlay 2 receivers in realtime mode use the same request byte (`0xd5`,
  shairport `rtp.c:1199`), but PipeWire only speaks AirPlay 1, so this doesn't
  matter for now.
