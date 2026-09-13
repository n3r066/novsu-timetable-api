# Schedule Knowledge

This directory stores human-reviewed presentation rules for timetable notes.
It is versioned in the private repository so formatting remains deterministic
across servers and deployments.

`schedule_notes.json` controls which common locations and short conditions are
shown directly in a lesson table. Unknown or complex notes are never discarded:
they remain in the collapsible `Важно` section for the corresponding day.

Add a value only after checking how the NovSU portal uses it. Runtime state and
"seen once" caches do not belong here because they would make the same snapshot
render differently on different machines.
