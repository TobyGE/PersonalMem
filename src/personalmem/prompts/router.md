You are routing one new screen capture into a set of currently-open work threads. A "thread" is a **topic** — a coherent task / problem / conversation / line of thought the user is pursuing. Threads are NOT bound to apps, files, time windows, or chat partners. The same topic can span many apps and many sub-views; the same window can host many topics over time.

Your job: read the new capture, read the **full activity history** of each open thread (every capture routed to it so far, in chronological order), and decide which topic the new capture belongs to.

## Open threads (most-recently-active first)

Each thread is shown with its title and the **complete list of captures** routed to it so far. Read the actual activities — what the user has been doing inside this thread — and match the new capture against that activity pattern, not just topical keywords.

{open_threads_block}

## New capture

```
timestamp:     {ts}
app:           {app}
window_title:  {window_title}
focused_role:  {focused_role}
focused_value: {focused_value}
url:           {url}
visible_text:  {visible_text}
```

## Principles

- **Topic continuity is what matters.** Surface markers (app, window title, chat partner, file name) are signals, not verdicts. Read the actual content of the new capture and the actual histories of the open threads.
- **Match by activity pattern, not by topical overlap.** If a thread has been editing OpenChronicle source code, running tests, and discussing the same project in chat, that's one *active project* thread. A YouTube video about AI / finance is *not* the same thread even though both touch on "AI" — the activity is fundamentally different (passive watching vs active making).
- **The same project usually spans many apps and files.** Editing different files in the same project, or a chat about that project, can all belong to one thread.
- **Different conversations are different threads, even on related topics.** A WeChat chat with person A is one thread; a chat with person B about a similar topic is another. Topical overlap is not enough to merge separate conversations.
- **Time gaps are weak signals.** A long gap doesn't break a thread; a short gap doesn't preserve one. The activity content is what counts.

## Decision options

- `continue` (with `thread_id`): the capture belongs to an already-open thread.
- `new`: the capture starts a new topic.

## Anti-noise

- UI chrome (browser address bar, extension icons, app sidebars) is not signal.
- If the capture has *no clear content* (e.g. a blank menu, unparseable visible_text, "Content not observed via accessibility" sentinel):
  - Prefer `continue` of an open thread **whose recent activity is in the SAME APP** as this capture.
  - If no open thread matches the app, prefer `new` — DO NOT continue an unrelated thread just because it's the most recent.

## Output

Return a JSON object with exactly these fields:

- `action`: one of `"continue"`, `"new"`
- `thread_id`: id of the thread to continue (required if action=continue), or `null` for `new`
- `reason`: one short sentence explaining your judgment, referencing the specific *activities* you compared
- `capture_description`: ONE concrete sentence (≤25 words) describing what the user is doing in **this new capture** specifically. Name the app + the topic/artifact + the action. This sentence is what future routing decisions will see as the "activity log" for this capture, so be specific about identifiable signals (paper title, repo name, chat partner, URL, file path, video title) — NOT topic-level abstractions like "AI research" or "WeChat chat".

Output only the JSON object, no markdown fences, no surrounding prose. Thread titles are managed by the summarizer separately — do not output a title.
