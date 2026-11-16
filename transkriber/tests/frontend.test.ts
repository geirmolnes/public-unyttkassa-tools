// Frontend smoke tests via Bun + happy-dom (using GlobalRegistrator so the
// DOM globals are available, then we execute transcript.js directly).
//
// The bug we're guarding against: transcript.js used to call renderTranscript()
// before `let segmentEls = []` was initialized, which threw a TDZ ReferenceError
// and silently killed the script. This test verifies the script runs clean and
// the DOM is populated correctly.

import { describe, expect, test, beforeEach, afterEach } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ROOT = join(import.meta.dir, "..");
const TRANSCRIPT_JS = readFileSync(join(ROOT, "src/static/transcript.js"), "utf-8");

function makeTranscriptPayload() {
    return {
        language: "no",
        segments: [
            { start_seconds: 0.0, end_seconds: 5.0,
              text: "Først, si fullt navn.", is_question: true },
            { start_seconds: 5.2, end_seconds: 9.5,
              text: "Ingjerd Østby Dahle.", is_question: false },
            { start_seconds: 9.6, end_seconds: 15.0,
              text: "Hvor lenge har du jobba på Spor?", is_question: true },
        ],
    };
}

function setupDOM(transcriptPayload: object | null) {
    if (transcriptPayload) {
        const audio = document.createElement("audio");
        audio.setAttribute("id", "audio");
        document.body.appendChild(audio);

        const article = document.createElement("article");
        article.setAttribute("id", "transcript");
        article.setAttribute("data-transcript", JSON.stringify(transcriptPayload));
        document.body.appendChild(article);

        const copy = document.createElement("button");
        copy.setAttribute("id", "copy-btn");
        copy.textContent = "Kopier tekst";
        document.body.appendChild(copy);

        const copyMenu = document.createElement("details");
        copyMenu.setAttribute("id", "copy-menu");
        for (const mode of ["plain", "stamps"]) {
            const opt = document.createElement("button");
            opt.className = "copy-opt";
            opt.setAttribute("data-copy-mode", mode);
            copyMenu.appendChild(opt);
        }
        document.body.appendChild(copyMenu);
    } else {
        const div = document.createElement("div");
        div.textContent = "Ingen transkripsjon";
        document.body.appendChild(div);
    }
}

beforeEach(() => {
    GlobalRegistrator.register();
});

afterEach(async () => {
    await GlobalRegistrator.unregister();
});

describe("transcript.js", () => {
    test("runs without errors when there is no transcript element", () => {
        setupDOM(null);
        expect(() => (0, eval)(TRANSCRIPT_JS)).not.toThrow();
    });

    test("renders one .segment per segment in the JSON payload", () => {
        const payload = makeTranscriptPayload();
        setupDOM(payload);
        (0, eval)(TRANSCRIPT_JS);

        const segments = document.querySelectorAll(".segment");
        expect(segments.length).toBe(payload.segments.length);
    });

    test("each segment contains its text and no speaker label", () => {
        const payload = makeTranscriptPayload();
        setupDOM(payload);
        (0, eval)(TRANSCRIPT_JS);

        const segments = Array.from(document.querySelectorAll(".segment"));
        segments.forEach((seg, i) => {
            const text = seg.querySelector(".segment-text")?.textContent ?? "";
            expect(text).toContain(payload.segments[i].text);
        });
        // Speaker markup was removed — questions are now italicised instead.
        expect(document.querySelectorAll(".speaker").length).toBe(0);
    });

    test("questions get the .is-question class and an <em> element", () => {
        const payload = makeTranscriptPayload();
        setupDOM(payload);
        (0, eval)(TRANSCRIPT_JS);

        const segments = Array.from(document.querySelectorAll(".segment"));
        segments.forEach((seg, i) => {
            const isQ = payload.segments[i].is_question;
            expect(seg.classList.contains("is-question")).toBe(isQ);
            const em = seg.querySelector(".segment-text em");
            if (isQ) {
                expect(em).not.toBeNull();
                expect(em?.textContent).toBe(payload.segments[i].text);
            } else {
                expect(em).toBeNull();
            }
        });
    });

    test("timestamps are rendered as data-time attributes on seek buttons", () => {
        // Timestamps rely on data-time + CSS ::before — without data-time
        // journalists wouldn't see any timestamps at all.
        setupDOM(makeTranscriptPayload());
        (0, eval)(TRANSCRIPT_JS);

        const seekButtons = document.querySelectorAll(".seek-btn");
        expect(seekButtons.length).toBeGreaterThan(0);
        seekButtons.forEach((btn) => {
            const dataTime = btn.getAttribute("data-time");
            expect(dataTime).toMatch(/^\d+:\d{2}(:\d{2})?$/);
        });
    });

    test("clicking a seek button updates audio.currentTime", () => {
        const payload = makeTranscriptPayload();
        setupDOM(payload);
        (0, eval)(TRANSCRIPT_JS);

        const audio = document.getElementById("audio") as HTMLAudioElement;
        const firstSeekBtn = document.querySelector(".seek-btn") as HTMLButtonElement;
        (audio as any).play = () => Promise.resolve();

        firstSeekBtn.click();
        expect(audio.currentTime).toBe(payload.segments[0].start_seconds);
    });

    test("copy-to-clipboard writes rich text/html with <em> on questions + clean text/plain", async () => {
        const payload = makeTranscriptPayload();
        setupDOM(payload);

        let writtenItems: any[] | null = null;
        let writeTextValue = "";
        Object.defineProperty(navigator, "clipboard", {
            configurable: true,
            value: {
                write: async (items: any[]) => { writtenItems = items; },
                writeText: async (s: string) => { writeTextValue = s; },
            },
        });

        (0, eval)(TRANSCRIPT_JS);

        const copyBtn = document.getElementById("copy-btn") as HTMLButtonElement;
        copyBtn.click();
        // Wait for the async click handler to read both Blobs.
        await new Promise((r) => setTimeout(r, 10));

        // Prefer clipboard.write (ClipboardItem) when available so rich
        // targets like Google Docs preserve the italics.
        expect(writtenItems).not.toBeNull();
        expect(writtenItems!.length).toBe(1);

        const item = writtenItems![0];
        const plain = await (await item.getType("text/plain")).text();
        const html = await (await item.getType("text/html")).text();

        // Plain view: no speaker labels, no timestamps, no italic markers.
        expect(plain).not.toContain("Speaker 1");
        expect(plain).not.toContain("_");
        expect(plain).not.toContain("*");
        expect(plain).not.toMatch(/\d+:\d{2}/);
        for (const seg of payload.segments) {
            expect(plain).toContain(seg.text);
        }

        // HTML view: journalist questions wrapped in <em>, answers not.
        for (const seg of payload.segments) {
            if (seg.is_question) {
                expect(html).toContain(`<em>${seg.text}</em>`);
            } else {
                expect(html).toContain(`<p>${seg.text}</p>`);
                expect(html).not.toContain(`<em>${seg.text}</em>`);
            }
        }

        // writeText was only the fallback path, so it shouldn't have fired.
        expect(writeTextValue).toBe("");
    });

    test("the timestamp menu item copies with [mm:ss] and becomes the default", async () => {
        setupDOM(makeTranscriptPayload());

        let writtenItems: any[] | null = null;
        Object.defineProperty(navigator, "clipboard", {
            configurable: true,
            value: {
                write: async (items: any[]) => { writtenItems = items; },
                writeText: async () => {},
            },
        });

        (0, eval)(TRANSCRIPT_JS);

        // Picking the variant copies straight away — no second click needed.
        (document.querySelector('[data-copy-mode="stamps"]') as HTMLButtonElement).click();
        await new Promise((r) => setTimeout(r, 10));

        const item = writtenItems![0];
        const plain = await (await item.getType("text/plain")).text();
        const html = await (await item.getType("text/html")).text();

        expect(plain).toContain("[00:00] Først, si fullt navn.");
        expect(plain).toContain("[00:05] Ingjerd Østby Dahle.");
        // Stamps sit outside the <em> so questions stay italic, times don't.
        expect(html).toContain("<p>[00:00] <em>Først, si fullt navn.</em></p>");
        expect(html).toContain("<p>[00:05] Ingjerd Østby Dahle.</p>");

        // …and the plain button now repeats that variant, here and next visit.
        expect(localStorage.getItem("transkriber.copyMode")).toBe("stamps");
        writtenItems = null;
        (document.getElementById("copy-btn") as HTMLButtonElement).click();
        await new Promise((r) => setTimeout(r, 10));
        expect(await (await writtenItems![0].getType("text/plain")).text())
            .toContain("[00:00] ");
    });

    test("copy defaults to plain, and the label never changes", () => {
        setupDOM(makeTranscriptPayload());
        (0, eval)(TRANSCRIPT_JS);

        expect(document.querySelector('[data-copy-mode="plain"]')!
            .classList.contains("is-active")).toBe(true);
        expect(document.getElementById("copy-btn")!.textContent).toBe("Kopier tekst");
    });

    test("diarized segments get clickable speaker labels with fallback names", () => {
        const payload = {
            language: "no",
            segments: [
                { start_seconds: 0, end_seconds: 2, text: "– Navn?",
                  is_question: true, speaker: "speaker_0" },
                { start_seconds: 2, end_seconds: 4, text: "– Kari.",
                  is_question: false, speaker: "speaker_1" },
                { start_seconds: 4, end_seconds: 6, text: "– Og alder?",
                  is_question: true, speaker: "speaker_0" },
            ],
        };
        setupDOM(payload);
        // Custom name for speaker_1 comes from the data-speakers attribute.
        document.getElementById("transcript")!
            .setAttribute("data-speakers", JSON.stringify({ speaker_1: "Kari Nordmann" }));

        (0, eval)(TRANSCRIPT_JS);

        const labels = Array.from(document.querySelectorAll(".speaker-label"));
        // One label per speaker TURN (0 → 1 → 0), not per segment.
        expect(labels.map((l) => l.textContent)).toEqual([
            "Taler 1", "Kari Nordmann", "Taler 1",
        ]);
    });

    test("gemini segments (no speaker field) render no speaker labels", () => {
        setupDOM(makeTranscriptPayload());
        (0, eval)(TRANSCRIPT_JS);
        expect(document.querySelectorAll(".speaker-label").length).toBe(0);
    });

    test("escapeHtml protects against HTML injection in segment text", async () => {
        const payload = {
            language: "no",
            segments: [
                { start_seconds: 0, end_seconds: 2,
                  text: "<script>alert(1)</script> & \"friends\"",
                  is_question: false },
            ],
        };
        setupDOM(payload);

        let writtenItems: any[] | null = null;
        Object.defineProperty(navigator, "clipboard", {
            configurable: true,
            value: {
                write: async (items: any[]) => { writtenItems = items; },
                writeText: async () => {},
            },
        });

        (0, eval)(TRANSCRIPT_JS);
        (document.getElementById("copy-btn") as HTMLButtonElement).click();
        await new Promise((r) => setTimeout(r, 10));

        const html = await (await writtenItems![0].getType("text/html")).text();
        expect(html).not.toContain("<script>");
        expect(html).toContain("&lt;script&gt;");
        expect(html).toContain("&amp;");
        expect(html).toContain("&quot;");
    });
});
