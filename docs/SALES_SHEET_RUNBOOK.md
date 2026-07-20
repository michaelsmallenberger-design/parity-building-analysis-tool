# Parity Building Analyzer: Sales Sheet Runbook

Use this guide when Sales sends a building list and the team needs to identify
cooling-tower opportunities and finish the same Google Sheet.

## What Sales Sends

Ask Sales for the **Google Sheet link**, not a downloaded copy, whenever possible.
The Sheet needs one column containing the building location. A normal header such
as `Address`, `Property Address`, `Street Address`, or `Building Address` is best.

Sales does **not** need to add formulas, change rows, or format anything for
Parity. Keep their original columns and row order intact.

Before the first run on a particular Sheet, its owner shares it as **Editor** with:

`sheet-writer@gen-lang-client-0702830838.iam.gserviceaccount.com`

That gives Parity permission to read the addresses and write the completed review
choices into those same rows. It does not make the Sheet public.

## The Normal Process

### 1. Open Parity

Open the Parity Building Analyzer public link and enter the team password. There
are no individual usernames. The password stays active in that browser for the
workday.

Only use the public link after the password protection has been activated in
Render. Do not send the team password outside the approved internal team.

### 2. Paste the Sheet Link

On the first screen:

1. Paste the Google Sheet link from Sales.
2. Click **Analyze my sheet**.

Do not upload a copy of the Sheet when the goal is to finish Sales's original
Sheet. File upload is only for an Excel/CSV file and creates a separate output
Sheet.

### 3. Confirm the Address Column, Only if Asked

For normal Sheets, Parity recognizes the address column and starts automatically.

If Sales uses unusual headers, Parity shows a short confirmation screen. Check:

- the correct Sheet tab;
- the street/address column; and
- city, state, and ZIP columns if they are shown.

Click **Use this setup and analyze the Sheet** only when the fields are correct.
At this point nothing has been analyzed and nothing has been written to the
Sheet. If the setup is wrong, go back rather than guessing.

### 4. Let Parity Analyze the Buildings

Parity processes each address in the background. It uses maps, building outlines,
and rooftop imagery to prepare a review card for each building.

The progress screen gives a link to the dark **Review Page**. You can keep that
link and return later through the Reviews page; progress is saved.

Do not move, delete, sort, or insert rows in the Sales Sheet while its batch is
running. This keeps every result connected to the correct original row.

### 5. Review Each Building

On the dark Review Page, each card shows the address, rooftop imagery, useful map
links, and the model's guidance. A person—not the model—makes the final choice.

For each building:

1. Look at the imagery and map links.
2. Select every HVAC system you can actually see, or select **None**.
3. Choose the team's Fit result.
4. Add a note only when it would help the next person.
5. Click **Submit**.

Each Submit immediately saves the reviewer choice and writes it back into the
same row in Sales's Google Sheet. The model guidance itself is not added as a
customer-facing Sheet column.

## Fit Standard

Use one Fit answer per building:

- `Optimizer`
- `Periscope`
- `None`
- `Not Clear`

Do not use the older `Good / Bad / Not Sure` choices. The review screen and Sheet
must be aligned to these four values before the first live Sales run.

## What Changes in the Sales Sheet

Parity never renames, rearranges, deletes, or overwrites the original Sales
columns. It also keeps existing formatting and existing dropdown rules.

If the Sheet already has the review columns, Parity fills only the matching cells
on the original rows. If required review columns are missing, Parity adds them at
the far right; it does not alter the existing layout.

## When the Work Is Complete

The run is complete when the Review Page count says every building has been
reviewed. Open the original Google Sheet and spot-check a few rows:

- the original Sales data is still present;
- HVAC/Fit/Notes appear on the intended rows; and
- the Sheet still looks like Sales's Sheet.

There is no second Sheet to merge or copy back when the Google Sheet link was
used. The Sales Sheet is the final output.

## If Something Does Not Look Right

| What you see | What to do |
| --- | --- |
| The wrong address column or Sheet tab is suggested | Do not confirm it. Go back and contact the Parity operator. |
| A Sheet says it needs setup | Check the address header and make sure the service account has Editor access. |
| A building has poor/no imagery | Mark the best human decision and add a short note; do not invent a result. |
| A Sheet value is marked invalid | Stop and tell the Parity operator; do not force a value outside the team dropdown. |
| A row was changed while a run was in progress | Stop the batch and have the Parity operator verify the row mapping before review continues. |

## Quick Checklist

- [ ] Sales provided the live Google Sheet link.
- [ ] The Sheet owner shared it with the Parity Sheet writer as Editor.
- [ ] The Sheet has a clear address column.
- [ ] The operator entered the team password and pasted the link.
- [ ] Any unusual address mapping was checked before confirming.
- [ ] Every review card was submitted by a person.
- [ ] The original Sheet was spot-checked after review.
