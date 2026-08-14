# Parity Building Analyzer: Sales Sheet Runbook

Use this guide when Sales sends a building list and the team needs to identify
cooling-tower opportunities and finish the same Google Sheet.

## What Sales Sends

Sales can send one `.xlsx` workbook containing several regional tabs or a live
Google Sheet link. A normal header such as `Address`, `Property Address`,
`Street Address`, or `Building Address` is best.

Sales does **not** need to add formulas, change rows, or format anything for
Parity. Keep their original columns and row order intact.

For a live Google Sheet, its owner shares it as **Editor** with:

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

### 2. Upload the Workbook or Paste the Sheet Link

On the first screen:

1. Upload Sales's `.xlsx` workbook, or paste the live Google Sheet link.
2. Start the analysis.

An `.xlsx` upload is converted into one new Google Sheet containing every
eligible regional tab. The Excel source is left unchanged. Legacy `.xls` files
must first be saved as `.xlsx`.

### 3. Confirm the Address Column, Only if Asked

For normal Sheets, Parity recognizes the address column and starts automatically.

Normal same-format regional tabs are included automatically. If Sales uses
unusual headers, a hidden address tab, or an ambiguous non-empty tab, Parity
stops the entire workbook on one confirmation screen before any paid analysis.
Check:

- every listed tab;
- the street/address column; and
- city, state, and ZIP columns if they are shown.

Confirm only when every address tab is accounted for. Empty instruction tabs
may be ignored. If the setup is wrong, go back rather than guessing.

Parity also verifies every existing Excel dropdown after conversion. If tab
structure, dropdown coverage, allowed values/ranges, or reject/warn behavior
changed, the converted copy is trashed and the run stops before analysis.

### 4. Let Parity Analyze the Buildings

Parity processes each address in the background. It uses maps, building outlines,
rooftop imagery, and available ground-level exterior imagery to prepare a review
card for each building.

The progress screen gives a link to the dark **Review Page**. You can keep that
link and return later through the Reviews page; progress is saved.

Do not move, delete, sort, or insert rows in the live Google Sheet while its
batch is running. Every result is tied to its original tab and physical row.

### 5. Review Each Building

On the dark Review Page, each card shows the address, rooftop imagery, useful map
links, and the model's guidance. A person—not the model—makes the final choice.

Cards show **Sheet Stories/Floors** only when the source Sheet contains a
recognized Stories or Floors column. Parity displays that source value; it does
not calculate or independently verify the number.

When the Sheet contains other approved building facts, the collapsed **Other
building information from Sheet** section can show property name/type, units,
building count, square footage, year built/renovated, building class,
construction type, city, and market. Owner/manager names, contacts, notes,
comments, and unrecognized columns are never copied into the review card.

The enlarged carousel labels the address-targeted ground view **Exterior / address** and,
when coverage exists, a second front/side context view **Exterior / alternate
angle**. If neither exterior image is available, the card says so and directs
the reviewer to the existing map links.

For each building:

1. Look at the imagery and map links.
2. Select every HVAC system you can actually see, or select **None**.
3. Choose **Optimizer Fit**.
4. Independently choose **Periscope Fit**.
5. Add a note only when it would help the next person.
6. Click **Submit**.

Each Submit immediately saves the reviewer choice and writes it back into the
same row in Sales's Google Sheet. The model guidance itself is not added as a
customer-facing Sheet column.

## Alex Review After Primary Completion

When the Alex review feature is enabled, it waits until every building has a
complete human HVAC plus both product-fit decisions and no Sheet write-back is
failed. It then places any building with **Maybe** in either product at the top
under **Needs Alex Review**. Current legacy runs also surface already-saved
**Okay** or **Not Sure** decisions in this queue. These cards remain counted as
reviewed; the separate Alex count is a follow-up queue, not incomplete primary
work. Parity changes only the review-page display order and never sorts the
Google Sheet.

For each Alex card:

1. Click **Review qualification**. HVAC remains locked.
2. Either adjust Optimizer Fit, Periscope Fit, and/or Notes and click **Save Alex
   review**, or click **Confirm current qualification** when the uncertainty is
   still correct.
3. Click **Cancel** to discard on-screen edits without saving anything.

A successful save or confirmation removes the card from the queue and returns
to the first review page. If another browser tab saved the card first, the stale
tab receives a conflict warning and writes nothing. If the local revision saves
but the Sheet update fails, the card moves to **Needs attention**; use **Retry
Sheet write-back**. The retry sends the same revision and does not create a
second history entry. A confirmation makes no Sheet call because no values
changed.

## Product-Fit Standard

Each building requires two independent product decisions:

- **Optimizer Fit:** `Customer`, `Good`, `Maybe`, or `Bad`
- **Periscope Fit:** `Customer`, `Good`, `Maybe`, or `Bad`

A building can be a good fit for both products, one product, or neither. The
reviewer therefore never combines these decisions into one field.

Use **Maybe** when the evidence is inconclusive or suggests a possible fit but
is not strong enough for **Good**; add a short note when imagery or location is
the reason. New runs store `Maybe` directly. For compatibility with current
legacy runs and their existing Sheet dropdowns, those pages still write the
established canonical value `Okay` while displaying it as **Maybe**. They do
not offer **Not Sure** as a new choice.

## What Changes in the Sales Sheet

Parity never renames, rearranges, deletes, or overwrites the original Sales
columns. It also keeps existing formatting and existing dropdown rules.

If the Sheet already has the review columns, Parity fills only the matching
cells on the original rows. Missing Parity review columns are appended at the
far right of each selected tab. Existing columns, formatting, and dropdown rules
are not reformatted or replaced.

## When the Work Is Complete

The run is complete only when every eligible row has a terminal machine result,
an HVAC decision plus both product-fit decisions have been submitted, and no
Sheet write-back remains failed. Open the resulting Google Sheet and spot-check
rows on several tabs:

- the original Sales data is still present;
- HVAC, Optimizer Fit, Periscope Fit, and Notes appear on the intended rows; and
- the Sheet still looks like Sales's Sheet.

There is no second Sheet to merge. A live Google Sheet input is updated in
place; an Excel input uses the one verified converted Google Sheet as its
canonical output.

## If Something Does Not Look Right

| What you see | What to do |
| --- | --- |
| The wrong address column or Sheet tab is suggested | Do not confirm it. Go back and contact the Parity operator. |
| A Sheet says it needs setup | Check the address header and make sure the service account has Editor access. |
| A building has poor/no imagery | Use **Maybe**, add a short note that imagery/location is insufficient, and do not invent a result. |
| Stories/Floors is blank | Confirm that the source Sheet contains a recognized `Stories` or `Floors` column. Parity does not estimate the value from imagery. |
| The card says no exterior view is available | Use the Google Maps, Google Earth, or Bing links directly below the imagery. |
| A Sheet value is marked invalid | Stop and tell the Parity operator; do not force a value outside the team dropdown. |
| A row was changed while a run was in progress | Stop the batch and have the Parity operator verify the row mapping before review continues. |
| An Alex review says the row changed in another tab | Reload the page and review the current saved values; the stale submission was not written. |
| An Alex revision appears under Needs attention | Use Retry Sheet write-back. The local revision is preserved and HVAC remains unchanged. |

## Quick Checklist

- [ ] Sales provided the live Google Sheet link.
- [ ] The Sheet owner shared it with the Parity Sheet writer as Editor.
- [ ] The Sheet has a clear address column.
- [ ] The operator entered the team password and pasted the link.
- [ ] Any unusual address mapping was checked before confirming.
- [ ] Every review card was submitted by a person.
- [ ] When enabled, the Needs Alex Review count reached zero or each remaining uncertainty was intentionally confirmed.
- [ ] The original Sheet was spot-checked after review.
