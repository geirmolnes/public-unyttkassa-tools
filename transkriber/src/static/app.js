// Upload flow only — after a job is 'done' we navigate to /t/{id}
// which does all the rendering. Keeps per-page code small.

const views = {
    upload: document.getElementById("upload-view"),
    progress: document.getElementById("progress-view"),
};

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");
const uploadError = document.getElementById("upload-error");
const progressText = document.getElementById("progress-text");
const progressFilename = document.getElementById("progress-filename");
const progressBarFill = document.getElementById("progress-bar-fill");
const progressBarContainer = document.getElementById("progress-bar-container");
const progressEta = document.getElementById("progress-eta");

function show(view) {
    for (const key of Object.keys(views)) {
        views[key].classList.toggle("hidden", key !== view);
    }
}

function showError(msg) {
    uploadError.textContent = msg;
    uploadError.classList.remove("hidden");
}

function clearError() {
    uploadError.textContent = "";
    uploadError.classList.add("hidden");
}

// ---- Drag & drop ----

["dragenter", "dragover"].forEach((ev) => {
    dropzone.addEventListener(ev, (e) => {
        e.preventDefault();
        dropzone.classList.add("drag-over");
    });
});

["dragleave", "drop"].forEach((ev) => {
    dropzone.addEventListener(ev, (e) => {
        e.preventDefault();
        dropzone.classList.remove("drag-over");
    });
});

dropzone.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files?.[0];
    if (file) startUpload(file);
});

fileInput.addEventListener("change", (e) => {
    const file = e.target.files?.[0];
    if (file) startUpload(file);
});

// ---- Progress bar ----

const UPLOAD_BAR_CEILING = 15;
const TRANSCRIBE_BAR_CEILING = 95;
const REALTIME_FACTOR = 8;
const MIN_TRANSCRIBE_SECONDS = 8;

let progressState = { value: 0 };

function setProgress(value) {
    progressState.value = value;
    const pct = Math.max(0, Math.min(100, value));
    progressBarFill.style.width = `${pct}%`;
    progressBarContainer.setAttribute("aria-valuenow", Math.round(pct));
}

function formatSeconds(s) {
    if (!isFinite(s) || s < 0) return "";
    if (s < 60) return `${Math.round(s)} s`;
    const m = Math.floor(s / 60);
    const sec = Math.round(s % 60);
    return `${m} min ${sec} s`;
}

function probeDuration(file) {
    return new Promise((resolve) => {
        const url = URL.createObjectURL(file);
        const audio = new Audio();
        audio.preload = "metadata";
        // Some codec/browser combos fire neither loadedmetadata nor error.
        // Without the timeout the upload flow would await this forever and
        // never start polling — even though the server-side job runs fine.
        let settled = false;
        const settle = (d) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            URL.revokeObjectURL(url);
            resolve(d);
        };
        const timer = setTimeout(() => settle(0), 5000);
        audio.addEventListener("loadedmetadata", () => {
            settle(isFinite(audio.duration) ? audio.duration : 0);
        });
        audio.addEventListener("error", () => settle(0));
        audio.src = url;
    });
}

// ---- Upload flow ----

async function startUpload(file) {
    clearError();

    // Reject oversized files before uploading anything — the server would
    // refuse them anyway, but only after the whole upload has been sent.
    const maxMb = parseInt(dropzone?.dataset.maxMb || "2048", 10);
    if (file.size > maxMb * 1024 * 1024) {
        showError(`Filen er større enn ${dropzone?.dataset.maxLabel || `${maxMb} MB`}`);
        return;
    }

    show("progress");
    progressFilename.textContent = file.name;
    progressText.textContent = "Laster opp...";
    progressEta.textContent = "";
    setProgress(0);

    const durationPromise = probeDuration(file);

    let jobId;
    try {
        jobId = await xhrUpload(file);
    } catch (err) {
        show("upload");
        showError(err.message);
        return;
    }

    const duration = await durationPromise;
    const estimateSeconds = Math.max(
        MIN_TRANSCRIBE_SECONDS,
        (duration || 60) / REALTIME_FACTOR,
    );
    progressText.textContent = "Transkriberer...";
    startTranscribeEstimate(estimateSeconds);

    pollJob(jobId);
}

function xhrUpload(file) {
    return new Promise((resolve, reject) => {
        const form = new FormData();
        form.append("file", file);

        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/upload");

        xhr.upload.addEventListener("progress", (e) => {
            if (!e.lengthComputable) return;
            const frac = e.loaded / e.total;
            setProgress(frac * UPLOAD_BAR_CEILING);
        });

        xhr.addEventListener("load", () => {
            if (xhr.status >= 200 && xhr.status < 300) {
                try {
                    const data = JSON.parse(xhr.responseText);
                    setProgress(UPLOAD_BAR_CEILING);
                    resolve(data.job_id);
                } catch {
                    reject(new Error("Uventet svar fra serveren"));
                }
            } else {
                let msg = "Opplasting feilet";
                try { msg = JSON.parse(xhr.responseText).detail || msg; } catch { /* */ }
                reject(new Error(msg));
            }
        });

        xhr.addEventListener("error", () => reject(new Error("Nettverksfeil")));
        xhr.addEventListener("abort", () => reject(new Error("Avbrutt")));

        xhr.send(form);
    });
}

let transcribeTicker = null;

function startTranscribeEstimate(expectedSeconds) {
    if (transcribeTicker) clearInterval(transcribeTicker);
    const startMs = performance.now();
    const startPct = progressState.value;
    const headroom = TRANSCRIBE_BAR_CEILING - startPct;

    transcribeTicker = setInterval(() => {
        const elapsed = (performance.now() - startMs) / 1000;
        const k = 1.9 / expectedSeconds;
        const fraction = 1 - Math.exp(-k * elapsed);
        const value = startPct + headroom * fraction;
        setProgress(value);

        const remaining = Math.max(0, expectedSeconds - elapsed);
        progressEta.textContent = remaining > 0
            ? `ca. ${formatSeconds(remaining)} igjen`
            : "Ferdigstiller...";
    }, 400);
}

function stopTranscribeEstimate() {
    if (transcribeTicker) {
        clearInterval(transcribeTicker);
        transcribeTicker = null;
    }
}

// How many consecutive failed status polls we tolerate before giving up.
// A single blip (laptop sleep, wifi handoff) must NOT dump the user back to
// the upload view — the job is still running server-side.
const MAX_POLL_FAILURES = 5;

async function pollJob(jobId) {
    let failures = 0;
    while (true) {
        await new Promise((r) => setTimeout(r, 2500));
        let data;
        try {
            const res = await fetch(`/status/${jobId}`);
            if (!res.ok) throw new Error("Kunne ikke hente status");
            data = await res.json();
            failures = 0;
        } catch {
            failures += 1;
            if (failures < MAX_POLL_FAILURES) continue;
            stopTranscribeEstimate();
            show("upload");
            showError(
                "Mistet kontakt med serveren — transkriberingen fortsetter i " +
                "bakgrunnen. Last inn siden på nytt for å finne den i sidemenyen.",
            );
            return;
        }
        if (data.status === "done") {
            stopTranscribeEstimate();
            setProgress(100);
            progressText.textContent = "Ferdig!";
            progressEta.textContent = "";
            // Navigate to the canonical transcript URL so the result is
            // bookmarkable and appears in the sidebar without a refresh.
            setTimeout(() => { window.location.href = `/t/${jobId}`; }, 300);
            return;
        }
        if (data.status === "error") {
            stopTranscribeEstimate();
            show("upload");
            showError(`Transkripsjon feilet: ${data.error}`);
            return;
        }
    }
}
