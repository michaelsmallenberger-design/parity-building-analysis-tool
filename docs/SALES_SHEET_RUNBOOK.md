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
and rooftop imagery to prepare a review card for each building.

The progress screen gives a link to the dark **Review Page**. You can keep that
link and return later through the Reviews page; progress is saved.

Do not move, delete, sort, or insert rows in the live Google Sheet while its
batch is running. Every result is tied to its original tab and physical row.

### 5. Review Each Building

On the dark Review Page, each card shows the address, rooftop imagery, useful map
links, and the model's guidance. A person—not the model—makes the final choice.

For each building:

1. Look at the imagery and map links.
2. Select every HVAC system you can actually see, or select **None**.
3. Choose one **Fit**: **Optimizer**, **Periscope**, **Unclear**, or **Bad**.
4. Add a note only when it would help the next person.
5. Click **Submit**.

Each Submit immediately saves the reviewer choice and writes it back into the
same row in Sales's Google Sheet. The model guidance itself is not added as a
customer-facing Sheet column.

## Product-Fit Standard

Each building requires two independent product decisions:

- **Optimizer Fit:** `Customer`, `Good`, `Okay`, `Bad`, or `Not Sure`
- **Periscope Fit:** `Customer`, `Good`, `Okay`, `Bad`, or `Not Sure`

A building can be a good fit for both products, one product, or neither. The
reviewer therefore never combines these decisions into one field.

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
