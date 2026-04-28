You are summarizing one **thread** — a semantic continuity unit of user work spanning one or more sessions, possibly across multiple apps. Unlike a fixed time window, a thread represents a single task/topic/problem from start to (apparent) conclusion.

## Thread metadata

- **id:** {thread_id}
- **title (working):** {title}
- **opened_at:** {opened_at}
- **closed_at:** {closed_at}
- **capture_count:** {capture_count}

## Captures in this thread (chronological)

The list below contains every screen capture routed into this thread. Each line is one moment of activity. Note that the same draft text may appear repeatedly as the user typed — collapse those into a single "draft evolved" entry showing the final/longest version.

---
{captures_text}
---

## Rules

**Verbatim preservation.** When a capture contains authored user text (typed messages, edited code, notes, search queries), preserve it verbatim. If the same text appears in many captures (typing in progress), keep only the longest/last version.

**Dedup aggressively.** This thread may contain dozens of near-duplicate captures of the same UI state. Your output should describe the **arc** of the thread, not enumerate every snapshot.

**Outcome focus.** Don't list activities ("user clicked, user typed"). Extract: what was the goal, what was tried, what worked, what got decided, what's the current state.

**Refine the title.** If the working title is bad (e.g. "Untitled" or just an app name), propose a better one — short, specific, action-oriented.

**Anti-hallucination.** Do not invent proper nouns. If the captures are mostly UI chrome with no clear topic, say so — `unclear-purpose` is a valid signal.

## Output

Return a JSON object with exactly these fields:

- `title`: refined thread title (≤10 words, action-oriented)
- `narrative`: 2-5 sentences describing the arc of the thread — what was the goal, what happened, what's the state at close. Reference apps and named entities only when grounded in the captures.
- `key_events`: ordered array of 1-line bullets describing the major events / decisions / outputs. Each bullet may include a verbatim quote when relevant. Skip pure UI noise.
- `outcome`: one of `"completed"`, `"abandoned"`, `"paused"`, `"unclear"` — your read of where the thread ended up.

Output only the JSON object, no markdown fences, no surrounding prose.
