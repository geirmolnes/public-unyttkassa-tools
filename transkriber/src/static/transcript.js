// Transcript detail page: render segments, audio playback, copy-to-clipboard.
// If the transcript is still processing, poll and reload on completion.

// Reload helper that forces a fresh fetch. window.location.reload() sometimes
// serves the cached HTML for same-origin same-URL loads, which leaves the
// page showing the pre-cleanup segments_json even though the server now has
// the cleaned version — exactly the "toggle only appears on manual refresh"
// symptom. A query-string bump bypasses both the memory cache and any disk
// cache without needing special cache headers.
function reloadFresh() {
    const url = new URL(window.location.href);
    url.searchParams.set("_t", Date.now().toString());
    window.location.replace(url.toString());
}

// Tidy the address bar after a reloadFresh: the _t cache-bust param has done
// its job once the page is loaded, and it shouldn't end up in bookmarks.
(function stripCacheBust() {
    try {
        const url = new URL(window.location.href);
        if (url.searchParams.has("_t")) {
            url.searchParams.delete("_t");
            history.replaceState(null, "", url.toString());
        }
    } catch { /* non-fatal cosmetics */ }
})();

const pollId = window.TRANSCRIBER_POLL_ID;
if (pollId) {
    // Auto-reload when the transcript finishes.
    (async function pollUntilDone() {
        while (true) {
            await new Promise((r) => setTimeout(r, 2500));
            try {
                const res = await fetch(`/status/${pollId}`);
                if (!res.ok) throw new Error("status fetch failed");
                const data = await res.json();
                if (data.status === "done" || data.status === "error") {
                    reloadFresh();
                    return;
                }
            } catch {
                // Transient error — keep polling.
            }
        }
    })();
}

const postPollId = window.TRANSCRIBER_POST_POLL_ID;
if (postPollId) {
    // Transcript is already rendered; wait for the background post-passes
    // (question detection + filler cleanup) to finish so italics appear and
    // the clean-toggle becomes available. The update is patched in WITHOUT a
    // page reload — the user may be minutes into playback by now, and a
    // reload would throw away their position.
    (async function pollPostPasses() {
        while (true) {
            await new Promise((r) => setTimeout(r, 3500));
            try {
                const res = await fetch(`/status/${postPollId}`);
                if (!res.ok) continue;
                const data = await res.json();
                if (data.questions_done && data.cleaned_done) {
                    const fullRes = await fetch(`/status/${postPollId}?full=1`);
                    if (!fullRes.ok) { reloadFresh(); return; }
                    const full = await fullRes.json();
                    if (full.transcript) {
                        applyTranscriptUpdate(full.transcript, full.speaker_names);
                    } else {
                        reloadFresh(); // shouldn't happen; fall back to the old path
                    }
                    return;
                }
            } catch {
                // Transient error — keep polling.
            }
        }
    })();
}

const transcriptEl = document.getElementById("transcript");

function formatTime(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}

let segmentEls = [];
let transcriptPayload = null;
let clipper = null; // assigned by setupClipper(); segment buttons call into it

const SCISSORS_SVG =
    '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor"' +
    ' stroke-width="1.5" aria-hidden="true">' +
    '<circle cx="4" cy="4.5" r="2"/><circle cx="4" cy="11.5" r="2"/>' +
    '<path d="M5.7 5.7 13.5 13.5M5.7 10.3 13.5 2.5"/></svg>';

// "cleaned" mode uses seg.cleaned_text when available; "raw" always uses seg.text.
// Defaults to cleaned if any segment has cleaned_text, so users see the curated
// version first but can flip back to verbatim with one click.
const CLEAN_PREF_KEY = "transkriber.viewMode";
const SPEAKER_PREF_KEY = "transkriber.showSpeakers";
let viewMode = localStorage.getItem(CLEAN_PREF_KEY) || "cleaned";

// Diarization display names (speaker_id -> user-chosen name), maintained
// server-side per transcript. Missing ids fall back to "Taler N".
let speakerNames = {};
try {
    speakerNames = JSON.parse(transcriptEl?.dataset.speakers || "{}") || {};
} catch { speakerNames = {}; }

function hasCleanedText(payload) {
    return (payload?.segments || []).some(
        (s) => s.cleaned_text !== null && s.cleaned_text !== undefined,
    );
}

function textFor(seg) {
    if (viewMode === "cleaned" && seg.cleaned_text !== null && seg.cleaned_text !== undefined) {
        return seg.cleaned_text;
    }
    return seg.text;
}

// First-appearance order of speaker ids, for the "Taler N" fallback labels.
function buildSpeakerOrder(payload) {
    const order = new Map();
    for (const seg of payload?.segments || []) {
        if (seg.speaker != null && !order.has(seg.speaker)) {
            order.set(seg.speaker, order.size + 1);
        }
    }
    return order;
}

function speakerDisplayName(id, order) {
    return speakerNames[id] || `Taler ${order.get(id) ?? "?"}`;
}

function makeSpeakerLabel(id, order) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "speaker-label";
    btn.textContent = speakerDisplayName(id, order);
    btn.title = "Klikk for å gi taleren et navn";
    btn.addEventListener("click", async () => {
        const name = prompt("Navn på taler:", speakerNames[id] || "");
        if (name === null) return; // cancelled
        const jobId = transcriptEl.dataset.jobId;
        try {
            const res = await fetch(`/t/${jobId}/speakers`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ speaker_id: id, name: name.trim() }),
            });
            if (!res.ok) throw new Error();
            if (name.trim()) speakerNames[id] = name.trim();
            else delete speakerNames[id]; // empty input reverts to "Taler N"
            renderTranscript();
        } catch {
            alert("Kunne ikke lagre navnet — prøv igjen.");
        }
    });
    return btn;
}

function renderTranscript() {
    if (!transcriptPayload) {
        transcriptPayload = JSON.parse(transcriptEl.dataset.transcript);
    }
    // May be null when the retention sweep has deleted the audio — the
    // transcript still renders, just without seek/clip interactions.
    const audioEl = document.getElementById("audio");
    transcriptEl.replaceChildren();
    segmentEls = [];

    const speakerOrder = buildSpeakerOrder(transcriptPayload);
    let prevSpeaker = null;

    for (const seg of transcriptPayload.segments) {
        // A diarized speaker turn (ElevenLabs only — Gemini segments carry
        // no speaker) gets its name tucked into the timestamp gutter of its
        // first segment, not a full-width row of its own.
        const startsTurn = seg.speaker != null && seg.speaker !== prevSpeaker;
        prevSpeaker = seg.speaker;

        const wrap = document.createElement("div");
        wrap.className = "segment";
        if (startsTurn) wrap.classList.add("turn-start");
        if (seg.is_question) wrap.classList.add("is-question");
        wrap.dataset.start = seg.start_seconds;
        wrap.dataset.end = seg.end_seconds;

        const btn = document.createElement("button");
        btn.className = "seek-btn";
        btn.type = "button";
        btn.dataset.time = formatTime(seg.start_seconds);
        btn.setAttribute("aria-label", `Spill av fra ${formatTime(seg.start_seconds)}`);
        btn.addEventListener("click", () => {
            if (!audioEl) return;
            audioEl.currentTime = seg.start_seconds;
            audioEl.play();
        });

        const text = document.createElement("div");
        text.className = "segment-text";
        // Questions render as <em> so they're visibly italicised in the UI.
        // Plain-text clipboard copy picks up the same text (no italic markers).
        const body = seg.is_question ? document.createElement("em") : document.createElement("span");
        body.textContent = textFor(seg);
        text.appendChild(body);

        // Clicking the text seeks+plays the segment, mirroring the timestamp
        // button. Bail when there's an active text selection so finishing a
        // drag-to-select (which fires a click on mouseup) doesn't hijack
        // the user's copy attempt.
        text.addEventListener("click", () => {
            if (!audioEl) return;
            const sel = window.getSelection();
            if (sel && sel.toString().length > 0) return;
            audioEl.currentTime = seg.start_seconds;
            audioEl.play();
        });

        // Scissors button opens the clip tool anchored on this segment's
        // time range. Hover-revealed on desktop (CSS), always visible on
        // touch devices where there is no hover.
        const clipBtn = document.createElement("button");
        clipBtn.className = "clip-btn";
        clipBtn.type = "button";
        clipBtn.title = "Lag lydklipp";
        clipBtn.setAttribute(
            "aria-label",
            `Lag lydklipp fra ${formatTime(seg.start_seconds)}`,
        );
        clipBtn.innerHTML = SCISSORS_SVG;
        clipBtn.addEventListener("click", () => {
            clipper?.open(seg.start_seconds, seg.end_seconds);
        });

        if (startsTurn) wrap.appendChild(makeSpeakerLabel(seg.speaker, speakerOrder));
        wrap.appendChild(btn);
        wrap.appendChild(text);
        wrap.appendChild(clipBtn);
        transcriptEl.appendChild(wrap);
        segmentEls.push(wrap);
    }

    // Re-render (Renset/Rå toggle) rebuilds segmentEls, dropping the
    // .in-clip highlights — reapply them if the clip tool is open.
    clipper?.sync();
}

// Applied when the post-passes finish: swap in the updated segments without
// reloading the page, so playback position and scroll survive.
function applyTranscriptUpdate(payload, names) {
    transcriptPayload = payload;
    if (names) speakerNames = names;
    updateToggleVisibility(); // recompute viewMode now that cleaned_text may exist
    renderTranscript();
    syncExportLinks();
    document.getElementById("post-processing-indicator")?.remove();
}

// Active-segment highlighting + auto-follow scroll. Lives OUTSIDE
// renderTranscript on purpose: re-renders (Renset/Rå toggle) used to add a
// duplicate timeupdate listener per toggle. Attached once; reads the current
// segmentEls through the module variable.
//
// Following is default behaviour, not a setting: the transcript tracks
// playback. Scrolling away manually suspends it (no yanking the view back
// mid-read); any playback interaction — play, seek, clicking a segment —
// re-engages it.
let followSuspended = false;
let lastActiveEl = null;
// True while a clip handle is being dragged. Dragging plays a short audio
// preview so you can place the edge by ear — but the transcript must NOT
// auto-scroll to the playing line during that, or the page slides under your
// finger and the handle jumps away mid-drag.
let clipScrubbing = false;

function setupActiveSegmentTracking() {
    const audioEl = document.getElementById("audio");
    if (!audioEl) return;

    function suspendFollow() {
        // Scrolling while paused is just reading — nothing to suspend.
        if (!audioEl.paused) followSuspended = true;
    }
    window.addEventListener("wheel", suspendFollow, { passive: true });
    window.addEventListener("touchmove", suspendFollow, { passive: true });
    // Re-engage on play/seek, and forget the last-seen segment so the very
    // next playing tick scrolls to wherever playback now is — without this,
    // seeking while paused marks the segment as "seen" and pressing play
    // wouldn't scroll until the NEXT segment boundary.
    function reengageFollow() {
        followSuspended = false;
        lastActiveEl = null;
    }
    audioEl.addEventListener("play", reengageFollow);
    // Fires for every user-initiated jump: segment clicks, the seek bar,
    // arrow keys, the clip tool. All of them mean "take me there".
    audioEl.addEventListener("seeking", reengageFollow);

    audioEl.addEventListener("timeupdate", () => {
        const t = audioEl.currentTime;
        // Don't mark any segment active until playback has actually begun.
        // Browsers fire a timeupdate at t=0 when metadata loads, which would
        // light up the first segment (start=0) and falsely imply we're already
        // a little way into the interview before the user has pressed play.
        const playing = t > 0 || !audioEl.paused;
        let activeEl = null;
        for (const el of segmentEls) {
            const start = parseFloat(el.dataset.start);
            const end = parseFloat(el.dataset.end);
            const active = playing && t >= start && t < end;
            el.classList.toggle("active", active);
            const btn = el.querySelector(".seek-btn");
            if (btn) btn.classList.toggle("playing", active);
            if (active) activeEl = el;
        }
        if (activeEl && activeEl !== lastActiveEl && !followSuspended
                && !clipScrubbing && !audioEl.paused) {
            activeEl.scrollIntoView({ block: "center", behavior: "smooth" });
        }
        if (activeEl) lastActiveEl = activeEl;
    });
}

function syncToggleButtons() {
    const btnCleaned = document.getElementById("toggle-cleaned");
    const btnRaw = document.getElementById("toggle-raw");
    if (!btnCleaned || !btnRaw) return;
    btnCleaned.setAttribute("aria-pressed", viewMode === "cleaned" ? "true" : "false");
    btnRaw.setAttribute("aria-pressed", viewMode === "raw" ? "true" : "false");
    btnCleaned.classList.toggle("active", viewMode === "cleaned");
    btnRaw.classList.toggle("active", viewMode === "raw");
}

// Show/hide the Renset/Rå toggle to match the CURRENT payload. Separate from
// setupViewToggle (which wires listeners exactly once) because the in-place
// post-pass update can make cleaned text appear after initial load.
function updateToggleVisibility() {
    const group = document.getElementById("clean-toggle-group");
    if (!group || !transcriptPayload) return;
    if (!hasCleanedText(transcriptPayload)) {
        // No cleaned text on this transcript (cleanup didn't run or failed).
        // Toggle stays hidden and viewMode is forced to raw.
        viewMode = "raw";
        group.classList.add("hidden");
    } else {
        viewMode = localStorage.getItem(CLEAN_PREF_KEY) || "cleaned";
        group.classList.remove("hidden");
    }
    syncToggleButtons();
}

function setupViewToggle() {
    const btnCleaned = document.getElementById("toggle-cleaned");
    const btnRaw = document.getElementById("toggle-raw");
    if (!btnCleaned || !btnRaw) return;

    function select(mode) {
        if (viewMode === mode) return;
        viewMode = mode;
        try { localStorage.setItem(CLEAN_PREF_KEY, mode); } catch { /* */ }
        syncToggleButtons();
        syncExportLinks();
        renderTranscript();
    }

    btnCleaned.addEventListener("click", () => select("cleaned"));
    btnRaw.addEventListener("click", () => select("raw"));
    updateToggleVisibility();
}

// Custom audio player. The native <audio controls> timeline can't be
// recoloured cross-browser (closed shadow DOM), so we hide it and drive our
// own high-contrast bar — accent fill for what's played, light track for
// what's ahead — plus play/pause, times and a playback-speed cycle.
function setupPlayer() {
    const audioEl = document.getElementById("audio");
    const playBtn = document.getElementById("play-btn");
    const track = document.getElementById("seek");
    const fill = document.getElementById("seek-fill");
    const curTime = document.getElementById("cur-time");
    const durTime = document.getElementById("dur-time");
    const rateBtn = document.getElementById("rate-btn");
    if (!audioEl || !playBtn || !track) return;

    // Reflect play/pause state via a class — SVG elements don't honour the
    // .hidden property, so we toggle .is-playing and let CSS swap the icons.
    function reflectPlaying(isPlaying) {
        playBtn.classList.toggle("is-playing", isPlaying);
        playBtn.setAttribute("aria-label", isPlaying ? "Pause" : "Spill av");
    }
    function setDuration() {
        durTime.textContent = isFinite(audioEl.duration) ? formatTime(audioEl.duration) : "0:00";
    }

    playBtn.addEventListener("click", () => {
        if (audioEl.paused) audioEl.play();
        else audioEl.pause();
    });
    audioEl.addEventListener("play", () => reflectPlaying(true));
    audioEl.addEventListener("pause", () => reflectPlaying(false));
    audioEl.addEventListener("ended", () => reflectPlaying(false));

    // Resume where the user left off last time they had this transcript
    // open. Skipped for barely-started or nearly-finished sessions, where
    // starting from 0:00 is less surprising.
    const jobId = transcriptEl?.dataset.jobId;
    const posKey = jobId ? `transkriber.pos.${jobId}` : null;
    function restorePosition() {
        if (!posKey) return;
        let saved = NaN;
        try { saved = parseFloat(localStorage.getItem(posKey) || ""); } catch { /* */ }
        if (
            isFinite(saved) && saved > 10 &&
            isFinite(audioEl.duration) && saved < audioEl.duration - 5
        ) {
            audioEl.currentTime = saved;
        }
    }
    function savePosition() {
        if (!posKey) return;
        try { localStorage.setItem(posKey, String(audioEl.currentTime)); } catch { /* */ }
    }

    audioEl.addEventListener("loadedmetadata", () => {
        setDuration();
        restorePosition();
    });
    if (audioEl.readyState >= 1) {
        setDuration(); // metadata may already be in
        restorePosition();
    }
    audioEl.addEventListener("pause", savePosition);
    audioEl.addEventListener("ended", () => {
        if (posKey) {
            try { localStorage.removeItem(posKey); } catch { /* */ }
        }
    });

    let lastPosSave = 0;
    audioEl.addEventListener("timeupdate", () => {
        const d = audioEl.duration;
        const pct = isFinite(d) && d > 0 ? (audioEl.currentTime / d) * 100 : 0;
        fill.style.width = `${pct}%`;
        curTime.textContent = formatTime(audioEl.currentTime);
        track.setAttribute("aria-valuenow", Math.round(pct));
        // Throttled position bookmark while playing (pause saves exactly).
        const now = performance.now();
        if (!audioEl.paused && now - lastPosSave > 3000) {
            lastPosSave = now;
            savePosition();
        }
    });

    // Seek by click or drag; pointer events cover mouse and touch alike.
    function seekToClientX(clientX) {
        const rect = track.getBoundingClientRect();
        const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
        if (isFinite(audioEl.duration)) audioEl.currentTime = frac * audioEl.duration;
    }
    let dragging = false;
    track.addEventListener("pointerdown", (e) => {
        dragging = true;
        seekToClientX(e.clientX);
        // After the seek: capture can throw for already-released pointers,
        // and the click-seek must land regardless.
        try { track.setPointerCapture(e.pointerId); } catch (err) { /* */ }
    });
    track.addEventListener("pointermove", (e) => {
        if (dragging) seekToClientX(e.clientX);
    });
    function endDrag(e) {
        dragging = false;
        try { track.releasePointerCapture(e.pointerId); } catch (err) { /* */ }
    }
    track.addEventListener("pointerup", endDrag);
    track.addEventListener("pointercancel", endDrag);
    track.addEventListener("keydown", (e) => {
        if (!isFinite(audioEl.duration)) return;
        if (e.key === "ArrowRight") {
            audioEl.currentTime = Math.min(audioEl.duration, audioEl.currentTime + 5);
            e.preventDefault();
        } else if (e.key === "ArrowLeft") {
            audioEl.currentTime = Math.max(0, audioEl.currentTime - 5);
            e.preventDefault();
        }
    });

    // Playback-speed cycle — replaces the speed option from the native menu.
    const RATES = [1, 1.25, 1.5, 1.75, 2, 0.75];
    let rateIdx = 0;
    if (rateBtn) {
        rateBtn.addEventListener("click", () => {
            rateIdx = (rateIdx + 1) % RATES.length;
            audioEl.playbackRate = RATES[rateIdx];
            rateBtn.textContent = `${RATES[rateIdx]}×`;
        });
    }

    // Global transport shortcuts: space = play/pause, arrows = ±5 s. Skipped
    // when focus is on an interactive element so buttons/sliders keep their
    // native key behaviour (and typing in future inputs stays unaffected).
    document.addEventListener("keydown", (e) => {
        if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
        const t = e.target;
        if (
            t instanceof Element &&
            t.closest("input, textarea, select, button, a, summary, [contenteditable='true'], [role='slider']")
        ) return;
        if (e.key === " ") {
            e.preventDefault(); // keep the page from scrolling
            if (audioEl.paused) audioEl.play();
            else audioEl.pause();
        } else if (e.key === "ArrowRight" && isFinite(audioEl.duration)) {
            e.preventDefault();
            audioEl.currentTime = Math.min(audioEl.duration, audioEl.currentTime + 5);
        } else if (e.key === "ArrowLeft") {
            e.preventDefault();
            audioEl.currentTime = Math.max(0, audioEl.currentTime - 5);
        }
    });
}

// Clip tool: a trim timeline scoped to the clip plus a margin of context. Two
// handles set the in/out points (drag = fine control); the ±5s buttons jump
// coarsely and re-frame the window. The accent band between the handles is the
// selection; a playhead line tracks preview playback. Listening before
// downloading matters because segment timestamps come from the transcription
// model and can drift a few seconds. The server cut via /clip/{id} is exact.
function setupClipper() {
    const bar = document.getElementById("clip-bar");
    const audioEl = document.getElementById("audio");
    const jobId = transcriptEl?.dataset.jobId;
    if (!bar || !audioEl || !jobId) return null;

    const startEl = document.getElementById("clip-start");
    const endEl = document.getElementById("clip-end");
    const curEl = document.getElementById("clip-cur");
    const lenEl = document.getElementById("clip-len");
    const playBtn = document.getElementById("clip-play");
    const track = document.getElementById("clip-track");
    const sel = document.getElementById("clip-sel");
    const playhead = document.getElementById("clip-playhead");
    const handleStart = document.getElementById("clip-handle-start");
    const handleEnd = document.getElementById("clip-handle-end");
    const bubbleStart = document.getElementById("clip-bubble-start");
    const bubbleEnd = document.getElementById("clip-bubble-end");
    const downloadEls = bar.querySelectorAll(".clip-download");
    const closeBtn = document.getElementById("clip-close");

    const STEP = 5; // seconds per extend/cut press
    const MIN_LEN = 1; // keeps the range from collapsing to nothing
    const MAX_CLIP_SECONDS = 600; // mirrors the server cap in main.py
    const errEl = document.getElementById("clip-error");
    let clipStart = 0;
    let clipEnd = 0;
    let playingClip = false; // audio is playing AS the clip (auto-stop at out-point)

    const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
    const audioDuration = () =>
        isFinite(audioEl.duration) ? audioEl.duration : Infinity;
    const isOpen = () => !bar.classList.contains("hidden");
    const clipLen = () => clipEnd - clipStart;

    // The timeline shows the clip plus a margin of context on each side, so a
    // handle drag can both extend (outward) and trim (inward). The window is
    // re-framed only on open and on the coarse ±5s jumps — never mid-drag, so a
    // handle tracks the pointer 1:1 instead of sliding as the scale shifts.
    let winStart = 0;
    let winEnd = 0;
    const winLen = () => winEnd - winStart;
    function recomputeWindow() {
        const pad = clamp(clipLen() * 0.3, 3, 30);
        winStart = Math.max(0, clipStart - pad);
        const dur = audioDuration();
        winEnd = isFinite(dur) ? Math.min(dur, clipEnd + pad) : clipEnd + pad;
        if (winLen() < MIN_LEN) winEnd = winStart + Math.max(clipLen(), MIN_LEN);
    }
    const pctOf = (t) => (winLen() > 0 ? clamp((t - winStart) / winLen() * 100, 0, 100) : 0);

    function reflectPlaying() {
        playBtn.classList.toggle("is-playing", playingClip);
        playBtn.setAttribute("aria-label", playingClip ? "Pause klipp" : "Spill av klipp");
    }

    // Place the playhead line within the framed window; hide it when playback
    // (e.g. via the main player) sits outside the window entirely.
    function syncPlayhead() {
        const t = audioEl.currentTime;
        playhead.style.left = `${pctOf(t)}%`;
        // Hidden when playback is outside the framed window (e.g. the main
        // player is somewhere else in the recording).
        playhead.style.opacity = t >= winStart - 0.05 && t <= winEnd + 0.05 ? "1" : "0";
        curEl.textContent = formatTime(clamp(t - clipStart, 0, clipLen()));
    }

    function showClipError(msg) {
        if (!errEl) return;
        errEl.textContent = msg;
        errEl.classList.remove("hidden");
    }

    function clearClipError() {
        errEl?.classList.add("hidden");
    }

    function setHandleAria(el, min, max, now) {
        el.setAttribute("aria-valuemin", Math.round(min));
        el.setAttribute("aria-valuemax", Math.round(max));
        el.setAttribute("aria-valuenow", Math.round(now));
        el.setAttribute("aria-valuetext", formatTime(now));
    }

    function sync() {
        if (!isOpen()) return;
        clearClipError(); // range changed — last attempt's error is stale

        // Position the selection band, the two handles, and their time bubbles.
        const a = pctOf(clipStart);
        const b = pctOf(clipEnd);
        sel.style.left = `${a}%`;
        sel.style.right = `${100 - b}%`;
        handleStart.style.left = `${a}%`;
        handleEnd.style.left = `${b}%`;
        bubbleStart.textContent = formatTime(clipStart);
        bubbleEnd.textContent = formatTime(clipEnd);
        setHandleAria(handleStart, winStart, clipEnd - MIN_LEN, clipStart);
        setHandleAria(handleEnd, clipStart + MIN_LEN, endCap(), clipEnd);

        startEl.textContent = formatTime(clipStart);
        endEl.textContent = formatTime(clipEnd);
        lenEl.textContent = formatTime(clipLen());
        const href = `/clip/${jobId}?start=${clipStart.toFixed(2)}&end=${clipEnd.toFixed(2)}`;
        const label = `Last ned klipp (${Math.round(clipLen())}s)`;
        for (const el of downloadEls) {
            el.href = href;
            el.textContent = label;
        }
        for (const btn of bar.querySelectorAll(".clip-adj")) {
            btn.disabled = adjustIsNoop(btn.dataset.bound, btn.dataset.dir);
        }
        for (const el of segmentEls) {
            const s = parseFloat(el.dataset.start);
            const e = parseFloat(el.dataset.end);
            el.classList.toggle("in-clip", s < clipEnd && e > clipStart);
        }
        syncPlayhead();
    }

    function adjustIsNoop(bound, dir) {
        // The server rejects clips over MAX_CLIP_SECONDS — stop the user at
        // the cap here instead of letting them build a doomed range.
        if (dir === "extend" && clipLen() >= MAX_CLIP_SECONDS) return true;
        if (bound === "start") {
            return dir === "extend" ? clipStart <= 0 : clipLen() <= MIN_LEN;
        }
        return dir === "extend" ? clipEnd >= audioDuration() : clipLen() <= MIN_LEN;
    }

    function open(start, end) {
        if (playingClip) audioEl.pause(); // pause handler resets clip-play state
        clipStart = Math.max(0, start);
        clipEnd = clamp(end, clipStart + MIN_LEN, audioDuration());
        bar.classList.remove("hidden");
        recomputeWindow();
        sync();
    }

    function close() {
        if (playingClip) audioEl.pause();
        bar.classList.add("hidden");
        for (const el of segmentEls) el.classList.remove("in-clip");
    }

    function clipTick() {
        if (!playingClip) return;
        if (audioEl.currentTime >= clipEnd) {
            audioEl.pause(); // pause event resets the clip-play state
            return;
        }
        syncPlayhead(); // rAF-smooth playhead while the clip plays
        requestAnimationFrame(clipTick);
    }

    playBtn.addEventListener("click", () => {
        if (playingClip) {
            audioEl.pause();
            return;
        }
        // Resume from a paused position inside the clip; otherwise start at
        // the in-point. The 0.05 s slack treats "at the out-point" as done.
        const t = audioEl.currentTime;
        if (t < clipStart || t >= clipEnd - 0.05) {
            audioEl.currentTime = clipStart;
        }
        playingClip = true;
        reflectPlaying();
        audioEl.play();
        requestAnimationFrame(clipTick);
    });

    audioEl.addEventListener("pause", () => {
        if (playingClip) {
            playingClip = false;
            reflectPlaying();
        }
    });
    // Backstop for the rAF tick: browsers throttle requestAnimationFrame in
    // unfocused/background tabs, which would let clip playback sail past the
    // out-point. timeupdate keeps firing during playback (just coarser), so
    // the stop is merely less precise in that state instead of lost. Also
    // keeps the playhead tracking main-player playback while paused/closed.
    audioEl.addEventListener("timeupdate", () => {
        if (isOpen()) syncPlayhead(); // keep the playhead line tracking during playback
        if (playingClip && audioEl.currentTime >= clipEnd) audioEl.pause();
    });
    // A manual seek outside the clip range means the user has moved on —
    // stop treating playback as the clip (which would auto-pause at the
    // out-point mid-listen). Clip-initiated seeks land inside the range,
    // so they survive this check.
    audioEl.addEventListener("seeking", () => {
        if (!playingClip) return;
        if (audioEl.currentTime < clipStart - 0.25 || audioEl.currentTime > clipEnd) {
            playingClip = false;
            reflectPlaying();
        }
    });

    const timeFromClientX = (clientX) => {
        const rect = track.getBoundingClientRect();
        const frac = clamp((clientX - rect.left) / rect.width, 0, 1);
        return winStart + frac * winLen();
    };
    // The out-point can't pass the recording's end; if duration is still
    // unknown the window edge stands in for it.
    const endCap = () =>
        isFinite(audioDuration()) ? Math.min(winEnd, audioDuration()) : winEnd;

    // Drag a handle to set that boundary. The window is frozen during the drag,
    // so the grip tracks the pointer 1:1; clamping keeps the minimum length and
    // the server's max. To travel past the framed window, use the ±5s jumps.
    let dragHandle = null;

    // Audible preview while dragging, so you can place an edge by ear instead of
    // by eye: dragging the start plays from the in-point; dragging the end plays
    // (and loops) the last couple of seconds up to the out-point — that's the
    // bit that actually matters when you're deciding where to cut.
    const END_PREVIEW = 2; // seconds of run-up heard at the out-point
    let scrubbing = null; // 'start' | 'end' | null
    let lastScrubSeek = 0;
    const previewPoint = () =>
        scrubbing === "start" ? clipStart : Math.max(clipStart, clipEnd - END_PREVIEW);

    function moveHandle(clientX) {
        const t = timeFromClientX(clientX);
        if (dragHandle === "start") {
            clipStart = clamp(t, winStart, clipEnd - MIN_LEN);
            if (clipLen() > MAX_CLIP_SECONDS) clipStart = clipEnd - MAX_CLIP_SECONDS;
        } else {
            clipEnd = clamp(t, clipStart + MIN_LEN, endCap());
            if (clipLen() > MAX_CLIP_SECONDS) clipEnd = clipStart + MAX_CLIP_SECONDS;
        }
        sync();
        if (scrubbing) scrubSeek();
    }

    function startScrub(which) {
        if (playingClip) audioEl.pause(); // hand off from clip playback
        scrubbing = which;
        clipScrubbing = true; // freeze follow-along scroll for the drag
        lastScrubSeek = 0;
        audioEl.currentTime = previewPoint();
        audioEl.play().catch(() => { /* load/autoplay races are non-fatal */ });
        requestAnimationFrame(scrubTick);
    }
    function scrubSeek() {
        // Throttle re-seeks so the audio actually sounds between moves rather
        // than restarting on every pointer event.
        const now = performance.now();
        if (now - lastScrubSeek < 120) return;
        lastScrubSeek = now;
        audioEl.currentTime = previewPoint();
    }
    function scrubTick() {
        if (!scrubbing) return;
        // Loop the run-up so the out-point preview keeps repeating while you
        // fine-tune, and never spills past the cut.
        if (scrubbing === "end" && audioEl.currentTime >= clipEnd) {
            audioEl.currentTime = previewPoint();
        }
        syncPlayhead();
        requestAnimationFrame(scrubTick);
    }
    function stopScrub() {
        if (!scrubbing) return;
        scrubbing = null;
        clipScrubbing = false;
        audioEl.pause();
    }
    for (const [which, el] of [["start", handleStart], ["end", handleEnd]]) {
        el.addEventListener("pointerdown", (e) => {
            dragHandle = which;
            e.preventDefault();
            e.stopPropagation(); // a handle grab is not a track seek
            try { el.setPointerCapture(e.pointerId); } catch (err) { /* */ }
            startScrub(which);
        });
        el.addEventListener("pointermove", (e) => {
            if (dragHandle === which) moveHandle(e.clientX);
        });
        const release = (e) => {
            if (dragHandle === which) {
                dragHandle = null;
                stopScrub();
                recomputeWindow(); // re-centre so the next nudge keeps resolution
                sync();
            }
            try { el.releasePointerCapture(e.pointerId); } catch (err) { /* */ }
        };
        el.addEventListener("pointerup", release);
        el.addEventListener("pointercancel", release);
        // Arrow keys nudge the focused handle 1 s (5 s with Shift) — finer
        // keyboard control than the ±5s buttons.
        el.addEventListener("keydown", (e) => {
            const dir = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
            if (!dir) return;
            e.preventDefault();
            const step = dir * (e.shiftKey ? 5 : 1);
            if (which === "start") clipStart = clamp(clipStart + step, winStart, clipEnd - MIN_LEN);
            else clipEnd = clamp(clipEnd + step, clipStart + MIN_LEN, endCap());
            sync();
        });
    }

    // Clicking the bar itself (not a handle) moves the playhead within the clip.
    track.addEventListener("pointerdown", (e) => {
        if (e.target.closest(".clip-handle")) return;
        audioEl.currentTime = clamp(timeFromClientX(e.clientX), clipStart, clipEnd);
        syncPlayhead();
    });

    for (const btn of bar.querySelectorAll(".clip-adj")) {
        btn.addEventListener("click", () => {
            const extend = btn.dataset.dir === "extend";
            if (btn.dataset.bound === "start") {
                clipStart = extend
                    ? Math.max(0, clipStart - STEP, clipEnd - MAX_CLIP_SECONDS)
                    : Math.min(clipStart + STEP, clipEnd - MIN_LEN);
            } else {
                clipEnd = extend
                    ? Math.min(clipEnd + STEP, audioDuration(), clipStart + MAX_CLIP_SECONDS)
                    : Math.max(clipEnd - STEP, clipStart + MIN_LEN);
            }
            recomputeWindow(); // a coarse jump re-frames the timeline
            sync();
        });
    }

    closeBtn.addEventListener("click", close);

    // Download via fetch instead of letting the browser navigate the <a>:
    // a failed cut would otherwise dump the user on a raw JSON error page,
    // and server-side cutting can take seconds with no feedback.
    function filenameFromDisposition(cd) {
        const star = cd.match(/filename\*=(?:utf-8|UTF-8)''([^;]+)/);
        if (star) {
            try { return decodeURIComponent(star[1].trim()); } catch { /* */ }
        }
        const plain = cd.match(/filename="?([^";]+)"?/);
        return plain ? plain[1] : null;
    }

    // Desktop and mobile each have their own download anchor (.clip-download);
    // only one is visible per breakpoint, so bind the same fetch-to-blob
    // handler to each and act on whichever was clicked.
    for (const dl of downloadEls) {
        dl.addEventListener("click", async (e) => {
            e.preventDefault();
            if (dl.classList.contains("busy")) return;
            clearClipError();
            const originalText = dl.textContent;
            dl.classList.add("busy");
            dl.setAttribute("aria-disabled", "true");
            dl.textContent = "Klipper…";
            try {
                const res = await fetch(dl.href);
                if (!res.ok) {
                    let detail = "Klipping feilet";
                    try { detail = (await res.json()).detail || detail; } catch { /* */ }
                    throw new Error(detail);
                }
                const blob = await res.blob();
                const name =
                    filenameFromDisposition(res.headers.get("content-disposition") || "") || "klipp";
                const a = document.createElement("a");
                a.href = URL.createObjectURL(blob);
                a.download = name;
                a.click();
                setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
            } catch (err) {
                showClipError(err.message || "Klipping feilet");
            } finally {
                dl.classList.remove("busy");
                dl.removeAttribute("aria-disabled");
                dl.textContent = originalText;
            }
        });
    }

    return { open, sync };
}

// Keep export links pointing at the current view mode (Renset vs Rå), so the
// downloaded file matches what's on screen.
function syncExportLinks() {
    const menu = document.getElementById("export-menu");
    const jobId = transcriptEl?.dataset.jobId;
    if (!menu || !jobId) return;
    for (const a of menu.querySelectorAll("a[data-format]")) {
        a.href = `/t/${jobId}/export?format=${a.dataset.format}&mode=${viewMode}`;
    }
}

// Show/hide speaker-name headers. Only wired up for diarized (ElevenLabs)
// transcripts; the button stays hidden otherwise. Off collapses every turn
// back to the plain timestamp+text rows for a more compact read.
function setupSpeakerToggle() {
    const btn = document.getElementById("speaker-toggle");
    if (!btn || !transcriptPayload) return;
    const hasSpeakers = (transcriptPayload.segments || []).some((s) => s.speaker != null);
    if (!hasSpeakers) return;
    btn.classList.remove("hidden");

    let show = localStorage.getItem(SPEAKER_PREF_KEY) !== "off";
    function reflect() {
        transcriptEl.classList.toggle("speakers-hidden", !show);
        btn.setAttribute("aria-pressed", show ? "true" : "false");
        btn.classList.toggle("active", show);
    }
    btn.addEventListener("click", () => {
        show = !show;
        try { localStorage.setItem(SPEAKER_PREF_KEY, show ? "on" : "off"); } catch { /* */ }
        reflect();
    });
    reflect();
}

function setupExportMenu() {
    const menu = document.getElementById("export-menu");
    if (!menu) return;
    // <details> stays open after choosing — close it so the panel doesn't
    // hang over the transcript while the download runs.
    menu.addEventListener("click", (e) => {
        if (e.target instanceof Element && e.target.closest("a[data-format]")) {
            menu.removeAttribute("open");
        }
    });
    document.addEventListener("click", (e) => {
        if (
            menu.hasAttribute("open") &&
            e.target instanceof Element && !menu.contains(e.target)
        ) {
            menu.removeAttribute("open");
        }
    });
    syncExportLinks();
}

// Render after all declarations — avoids a temporal-dead-zone ReferenceError
// when renderTranscript tries to assign to `segmentEls`.
if (transcriptEl) {
    renderTranscript();
    setupViewToggle();
    setupPlayer();
    clipper = setupClipper();
    setupActiveSegmentTracking();
    setupSpeakerToggle();
    setupExportMenu();
}

// Which variant the plain copy button repeats. Plain by default — pasting
// into an article should give clean prose; picking a variant from the caret
// menu makes it the default for next time.
const COPY_MODE_PREF_KEY = "transkriber.copyMode";
let copyMode = localStorage.getItem(COPY_MODE_PREF_KEY) === "stamps" ? "stamps" : "plain";

// Builds both clipboard flavours: text/plain for terminals/editors, text/html
// so rich targets (Google Docs, Word, Gmail) keep the italics on the
// journalist's questions. Timestamps live in a CSS pseudo-element outside
// .segment-text, so they're never picked up implicitly — with stamps on we
// read them back off the segment's data-start and prefix each line.
function buildCopyPayload(withTimestamps) {
    const plainLines = [];
    const htmlLines = [];
    for (const el of segmentEls) {
        const text = (el.querySelector(".segment-text")?.textContent ?? "").trim();
        if (!text) continue;
        const stamp = withTimestamps
            ? `[${formatTime(Number(el.dataset.start) || 0)}] `
            : "";
        plainLines.push(stamp + text);
        const isQuestion = el.classList.contains("is-question");
        const safe = escapeHtml(text);
        const body = isQuestion ? `<em>${safe}</em>` : safe;
        htmlLines.push(`<p>${stamp}${body}</p>`);
    }
    return { plain: plainLines.join("\n\n"), html: htmlLines.join("\n") };
}

const copyBtn = document.getElementById("copy-btn");
if (copyBtn) {
    const copyMenu = document.getElementById("copy-menu");
    const modeBtns = document.querySelectorAll("[data-copy-mode]");
    const BASE_LABEL = copyBtn.textContent;

    // The menu marks which variant the main button repeats; the label itself
    // stays put so the action bar never reflows.
    const reflectCopyMode = () => {
        for (const b of modeBtns) {
            b.classList.toggle("is-active", b.dataset.copyMode === copyMode);
        }
    };

    async function copyTranscript(mode) {
        const { plain, html } = buildCopyPayload(mode === "stamps");
        try {
            if (window.ClipboardItem && navigator.clipboard.write) {
                const item = new ClipboardItem({
                    "text/plain": new Blob([plain], { type: "text/plain" }),
                    "text/html": new Blob([html], { type: "text/html" }),
                });
                await navigator.clipboard.write([item]);
            } else {
                // Safari < 13.4 / some Firefox configs: fall back to plain.
                await navigator.clipboard.writeText(plain);
            }
            copyBtn.textContent = "Kopiert!";
            setTimeout(() => (copyBtn.textContent = BASE_LABEL), 1500);
        } catch {
            copyBtn.textContent = "Kopiering feilet";
        }
    }

    copyBtn.addEventListener("click", () => copyTranscript(copyMode));

    for (const b of modeBtns) {
        b.addEventListener("click", () => {
            copyMode = b.dataset.copyMode;
            try {
                localStorage.setItem(COPY_MODE_PREF_KEY, copyMode);
            } catch { /* private mode — the choice still holds for this page */ }
            reflectCopyMode();
            copyMenu?.removeAttribute("open");
            copyTranscript(copyMode);
        });
    }

    // Clicking anywhere outside closes the panel, same as the download menu.
    document.addEventListener("click", (e) => {
        if (
            copyMenu?.hasAttribute("open") &&
            e.target instanceof Element && !copyMenu.contains(e.target)
        ) {
            copyMenu.removeAttribute("open");
        }
    });
    reflectCopyMode();
}

function escapeHtml(s) {
    return s
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}
