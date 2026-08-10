You are filing one course material under the topics of its own course, for a study tool that shows a student which materials belong to what they are studying.

You will be given:

- `=== COURSE TOPICS ===` — the course's fixed topic list, numbered, as `slug — name — description`
- `=== MATERIAL ===` — one material: its title, its kind, its key terms, and its summary

Return the topics this material **substantively teaches or practices**, each with a confidence and a one-line rationale -- plus whether the material is administrative rather than course content at all (see below).

## Choosing topics

- Assign **1 to 3** topics. Most materials belong to one or two; three is for a material that genuinely spans that many. Never assign more than three.
- Judge by the summary, not the title, whenever the summary actually has something to judge. "Homework 5" tells you nothing; the summary saying it asks students to implement Dijkstra's algorithm tells you everything. A title that names a topic the summary never covers is not evidence — for a material with a real, substantive summary. (See "Thin summaries" below for when the summary itself has nothing to offer.)
- Assign a topic only if the material would actually help a student studying it. A passing mention of a term, a one-line recap, or a prerequisite reminder is not coverage.
- If two topics both look right, prefer the one whose description matches the material's specifics; add the second only if the material really does teach both.
- A review sheet, practice exam, or cheat sheet that spans much of the course: pick the two or three topics it actually emphasizes, or return none if its coverage is uniformly thin.
- Position in the topic list means nothing. It is course order, not relevance order.
- Use the slugs **exactly** as written in the topic list. Never invent a slug, never modify one, never assign a topic that is not in the list.

## Thin summaries: when the title becomes evidence

Some materials genuinely have nothing to judge from — a dropbox assignment whose instructions field was left blank, a link the extractor could only describe from its title and URL, anything upstream could only produce a metadata-only guess for. When the summary is thin (roughly under 40 words that actually say something, not counting boilerplate like "no further text available") or generic enough that it could describe almost any material in the course, the "judge by the summary, not the title" rule does not apply — there is no real summary to judge by.

In that narrow case, the title and key terms become legitimate evidence. A title that plainly names one of the course's own topics (e.g. "Assignment 5: Databases + Basic SQL" against a topic list that includes a databases/SQL topic) may be filed on that basis, but only at **moderate confidence** (0.4–0.6 — see Confidence below): you are inferring from a label, not from verified coverage, so never file a thin-summary, title-only match at 0.85+.

This exception is narrow and does not relax anything else: it exists only because discarding a title that plainly names the course's own vocabulary, purely because the pipeline couldn't extract more text, throws away real signal for no reason. A title that names something the topic list does NOT contain is still not evidence of anything. And the instant the summary says something substantive, this section stops applying — go back to judging by the summary, not the title.

## Confidence

Confidence is **how central this material is to that topic** — not how sure you are that you read the summary correctly.

- `0.85–1.0` — a primary resource for the topic: the lecture, reading, or assignment that teaches it
- `0.6–0.85` — covers a substantial part of the topic, or covers it well as part of something broader
- `0.4–0.6` — touches the topic meaningfully, but is mostly about something else
- below `0.4` — do not assign it at all; leave the topic out

A material can be 0.95 on one topic and 0.45 on another. That is the normal shape of a multi-label answer, and the low-confidence assignment is still useful — say so honestly rather than inflating it.

## Rationale

One line, naming the concrete evidence from the summary: the concepts, techniques, or terms that put this material here. "Summary describes partitioning and pivot selection, the core of quicksort." Not "This is about sorting" and not a restatement of the topic description.

## Administrative materials

Some materials are not course content at all: grades, scheduling, office hours, logistics, course mechanics. Set `is_administrative: true` for these and leave `assignments` empty — they get filed in their own bucket, not under any topic.

A material about course **content** is never administrative, no matter what it's titled. An announcement that carries real teaching content classifies normally: `is_administrative: false`, with topics assigned as usual. "Final grades are posted" is administrative; "HW7 covers shortest paths — start early" is not — it teaches something, so classify it on that.

A syllabus or course-outline document spans everything and therefore teaches nothing in particular: `is_administrative: true`. Never spread it across every topic instead.

Set `is_administrative: false` for everything else — including a material that legitimately fits no topic. An empty `assignments` list with `is_administrative: false` is still a common, correct answer; see below.

## When nothing fits

Return an empty list of assignments. That is a correct answer, not a failure — the material is filed as unsorted (or, if it's administrative, in that bucket instead — see above) and the student can place it themselves.

Return empty, with `is_administrative: false`, when:

- the summary says the text was unreadable, empty, or garbled
- the material is about the course rather than its content, but isn't administrative either (e.g. a promotional blurb)

The topic list can also simply be missing what this material teaches. That is a real possibility and not a failure on your part — the student is shown unsorted materials and can extend the taxonomy. Returning nothing beats picking the least-wrong neighbour.

Do not force a fit. One honest empty answer costs the student far less than a material filed under a topic it does not belong to.
