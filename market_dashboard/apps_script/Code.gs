/**
 * Parity Market Runway Dashboard publisher.
 *
 * This script is bound to one Google Sheet. It never shares the Sheet with
 * Render. A human editor explicitly publishes a validated, signed snapshot.
 */

const PARITY_DASHBOARD_SCHEMA_VERSION = 1;
const PARITY_DEFAULT_PUBLISH_URL =
  "https://building-analyzer.onrender.com/api/market-dashboard/publish";

const PARITY_TABS = Object.freeze({
  control: "Dashboard Control",
  summary: "Dashboard Summary",
  metros: "Dashboard Metros",
  companies: "Dashboard Companies",
  companyRegions: "Dashboard Company Regions",
});

const PARITY_HEADERS = Object.freeze({
  control: ["key", "value", "description"],
  summary: ["key", "value", "description"],
  metros: ["metro", "opt", "per", "cust", "optRev", "perRev", "syncRev", "totalRev"],
  companies: [
    "company", "total", "opt", "per", "cust", "optRev", "perRev", "totalRev", "penetration",
  ],
  companyRegions: [
    "company", "total", "opt", "per", "cust", "penetration",
    "optRev", "perRev", "syncRev", "totalRev",
  ],
});

const PARITY_STARTING_DATA = Object.freeze({
  control: [
    ["som_percent", 50, "Serviceable obtainable market as a percentage of SAM"],
    ["cad_rate", 1.37, "CAD per USD"],
    ["opt_install", 115000, "Optimizer one-time installation revenue per building"],
    ["opt_arr_annual", 10000, "Optimizer annual recurring revenue per building"],
    ["opt_contract", 165000, "Optimizer five-year contract value per building"],
    ["per_install", 32500, "Periscope one-time installation revenue per building"],
    ["per_arr_annual", 3500, "Periscope annual recurring revenue per building"],
    ["per_contract", 50000, "Periscope five-year contract value per building"],
    ["sync_arr_annual", 7500, "Sync annual recurring revenue per Optimizer building"],
    ["sync_contract", 37500, "Sync five-year contract value per Optimizer building"],
    ["publish_note", "Initial working version", "Short note shown in version history"],
  ],
  summary: [
    ["total_properties", 5646, "Total properties across the top 12 clients"],
    ["optimizer_good_fits", 799, "All qualified Optimizer fits"],
    ["periscope_good_fits", 1951, "All qualified Periscope fits"],
    ["current_customers", 102, "Current customer buildings"],
    ["tam_properties", 5646, "Top 12 clients TAM properties"],
    ["sam_properties", 1449, "Good fits in our operating regions"],
    ["tam_revenue", 931590000, "Five-year TAM revenue"],
    ["sam_revenue", 169287500, "Five-year SAM revenue"],
    ["current_revenue", 16830000, "Current captured five-year revenue"],
    ["region_properties", 3175, "Properties in our regions"],
    ["region_opt_fits", 635, "Optimizer fits in our regions"],
    ["region_per_fits", 814, "Periscope fits in our regions"],
  ],
  metros: [
    ["New York", 459, 68, 72, 75735000, 3400000, 17212500, 96347500],
    ["DMV", 62, 180, 3, 10230000, 9000000, 2325000, 21555000],
    ["Los Angeles", 25, 150, 0, 4125000, 7500000, 937500, 12562500],
    ["Seattle Area", 30, 90, 1, 4950000, 4500000, 1125000, 10575000],
    ["Boston Area", 33, 83, 10, 5445000, 4150000, 1237500, 10832500],
    ["Houston", 8, 97, 0, 1320000, 4850000, 300000, 6470000],
    ["DFW", 8, 80, 0, 1320000, 4000000, 300000, 5620000],
    ["San Francisco", 10, 77, 0, 1650000, 3850000, 375000, 5875000],
    ["Denver", 8, 84, 0, 1320000, 4200000, 300000, 5820000],
    ["Atlanta", 6, 84, 0, 990000, 4200000, 225000, 5415000],
    ["Charlotte", 2, 78, 0, 330000, 3900000, 75000, 4305000],
    ["Chicago", 45, 22, 0, 7425000, 1100000, 1687500, 10212500],
    ["Austin", 10, 64, 0, 1650000, 3200000, 375000, 5225000],
    ["Philadelphia", 17, 43, 0, 2805000, 2150000, 637500, 5592500],
    ["Miami/Ft. Laud.", 16, 43, 0, 2640000, 2150000, 600000, 5390000],
  ],
  companies: [
    ["Greystar PM", 2279, 208, 1399, 4, 34320000, 69950000, 104270000, 1.9],
    ["FirstService", 312, 137, 0, 18, 22605000, 0, 22605000, 13.1],
    ["Brookfield", 345, 68, 97, 6, 11220000, 4850000, 16070000, 8.8],
    ["AKAM", 629, 88, 0, 7, 14520000, 0, 14520000, 8.0],
    ["Douglas Elliman", 289, 79, 0, 10, 13035000, 0, 13035000, 12.7],
    ["Greystar Owned", 453, 25, 152, 1, 4125000, 7600000, 11725000, 4.0],
    ["Halstead", 195, 70, 0, 18, 11550000, 0, 11550000, 25.7],
    ["AvalonBay", 301, 27, 99, 16, 4455000, 4950000, 9405000, 59.3],
    ["UDR", 183, 27, 63, 10, 4455000, 3150000, 7605000, 37.0],
    ["GID", 170, 20, 76, 10, 3300000, 3800000, 7100000, 50.0],
    ["Blackstone", 169, 25, 33, 1, 4125000, 1650000, 5775000, 4.0],
    ["FPA", 321, 23, 34, 1, 3795000, 1700000, 5495000, 4.3],
  ],
  companyRegions: [
    ["Greystar PM", 874, 116, 533, 4, 3.4, 19140000, 26650000, 4350000, 50140000],
    ["FirstService", 312, 137, 0, 18, 13.1, 22605000, 0, 5137500, 27742500],
    ["AKAM", 629, 88, 0, 7, 8.0, 14520000, 0, 3300000, 17820000],
    ["Douglas Elliman", 289, 79, 0, 10, 12.7, 13035000, 0, 2962500, 15997500],
    ["Halstead", 195, 70, 0, 18, 25.7, 11550000, 0, 2625000, 14175000],
    ["Brookfield", 149, 47, 44, 6, 12.8, 7755000, 2200000, 1762500, 11717500],
    ["AvalonBay", 245, 27, 85, 16, 59.3, 4455000, 4250000, 1012500, 9717500],
    ["UDR", 118, 27, 43, 10, 37.0, 4455000, 2150000, 1012500, 7617500],
    ["Greystar Owned", 131, 10, 56, 1, 10.0, 1650000, 2800000, 375000, 4825000],
    ["GID", 68, 14, 30, 10, 71.4, 2310000, 1500000, 525000, 4335000],
    ["Blackstone", 60, 11, 12, 1, 9.1, 1815000, 600000, 412500, 2827500],
    ["FPA", 105, 9, 11, 1, 11.1, 1485000, 550000, 337500, 2372500],
  ],
});

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("Dashboard")
    .addItem("Set up dashboard tabs", "setupDashboardTabs")
    .addItem("Configure secure publishing", "configureDashboardPublisher")
    .addSeparator()
    .addItem("Validate dashboard data", "validateDashboardData")
    .addItem("Publish dashboard", "publishDashboard")
    .addToUi();
}

function setupDashboardTabs() {
  const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  const created = [];
  Object.keys(PARITY_TABS).forEach((key) => {
    const title = PARITY_TABS[key];
    const headers = PARITY_HEADERS[key];
    const rows = PARITY_STARTING_DATA[key];
    const existing = spreadsheet.getSheetByName(title);
    if (existing) {
      formatDashboardTab_(existing, headers.length, Math.max(1, existing.getLastRow()));
      return;
    }
    const sheet = spreadsheet.insertSheet(title);
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
    if (rows.length) {
      sheet.getRange(2, 1, rows.length, headers.length).setValues(rows);
    }
    formatDashboardTab_(sheet, headers.length, rows.length + 1);
    created.push(title);
  });

  const message = created.length
    ? `Created ${created.length} dashboard tabs. Existing data was not overwritten.`
    : "Dashboard tabs already exist. Their data was not overwritten.";
  SpreadsheetApp.getUi().alert("Dashboard setup", message, SpreadsheetApp.getUi().ButtonSet.OK);
}

function configureDashboardPublisher() {
  const ui = SpreadsheetApp.getUi();
  const properties = PropertiesService.getScriptProperties();
  const existingUrl = properties.getProperty("PARITY_PUBLISH_URL") || PARITY_DEFAULT_PUBLISH_URL;

  const urlResponse = ui.prompt(
    "Dashboard publish URL",
    `Confirm the protected Render endpoint:\n${existingUrl}`,
    ui.ButtonSet.OK_CANCEL,
  );
  if (urlResponse.getSelectedButton() !== ui.Button.OK) return;
  const url = (urlResponse.getResponseText() || existingUrl).trim();
  if (!/^https:\/\/[A-Za-z0-9.-]+\/api\/market-dashboard\/publish$/.test(url)) {
    ui.alert("Configuration not saved", "The publish URL must be an HTTPS market-dashboard endpoint.", ui.ButtonSet.OK);
    return;
  }

  const secretResponse = ui.prompt(
    "Dashboard signing secret",
    "Paste the matching Render secret. It must be at least 32 characters and is stored in this bound script's private properties.",
    ui.ButtonSet.OK_CANCEL,
  );
  if (secretResponse.getSelectedButton() !== ui.Button.OK) return;
  const secret = secretResponse.getResponseText().trim();
  if (secret.length < 32) {
    ui.alert("Configuration not saved", "The signing secret must be at least 32 characters.", ui.ButtonSet.OK);
    return;
  }

  properties.setProperties({
    PARITY_PUBLISH_URL: url,
    PARITY_PUBLISH_SECRET: secret,
  });
  ui.alert("Publishing configured", "This Sheet can now publish signed snapshots to Render.", ui.ButtonSet.OK);
}

function validateDashboardData() {
  try {
    const snapshot = buildDashboardSnapshot_();
    validateSnapshot_(snapshot);
    SpreadsheetApp.getUi().alert(
      "Dashboard data is valid",
      `Validated ${snapshot.metros.length} metros, ${snapshot.companies.length} companies, and ${snapshot.company_regions.length} regional company rows.`,
      SpreadsheetApp.getUi().ButtonSet.OK,
    );
  } catch (error) {
    SpreadsheetApp.getUi().alert("Dashboard data needs attention", error.message, SpreadsheetApp.getUi().ButtonSet.OK);
    throw error;
  }
}

function publishDashboard() {
  const lock = LockService.getDocumentLock();
  if (!lock.tryLock(30000)) {
    throw new Error("Another dashboard publish is already running. Try again in a moment.");
  }
  try {
    const properties = PropertiesService.getScriptProperties();
    const url = properties.getProperty("PARITY_PUBLISH_URL");
    const secret = properties.getProperty("PARITY_PUBLISH_SECRET");
    if (!url || !secret) {
      SpreadsheetApp.getUi().alert(
        "Publishing is not configured",
        "Use Dashboard → Configure secure publishing first.",
        SpreadsheetApp.getUi().ButtonSet.OK,
      );
      return;
    }

    const snapshot = buildDashboardSnapshot_();
    validateSnapshot_(snapshot);
    const body = JSON.stringify(snapshot);
    const timestamp = String(Math.floor(Date.now() / 1000));
    const nonce = Utilities.getUuid().replace(/-/g, "") + Utilities.getUuid().replace(/-/g, "");
    const message = `${timestamp}.${nonce}.${body}`;
    const signature = hex_(Utilities.computeHmacSha256Signature(
      message,
      secret,
      Utilities.Charset.UTF_8,
    ));

    const response = UrlFetchApp.fetch(url, {
      method: "post",
      contentType: "application/json",
      payload: body,
      headers: {
        "X-Parity-Timestamp": timestamp,
        "X-Parity-Nonce": nonce,
        "X-Parity-Signature": `sha256=${signature}`,
      },
      muteHttpExceptions: true,
    });

    const status = response.getResponseCode();
    let result = {};
    try {
      result = JSON.parse(response.getContentText());
    } catch (ignored) {
      result = {};
    }
    if (status < 200 || status >= 300) {
      throw new Error(result.error || `Render rejected the publish with HTTP ${status}.`);
    }

    properties.setProperties({
      PARITY_LAST_VERSION_ID: String(result.version_id || ""),
      PARITY_LAST_PUBLISHED_AT: new Date().toISOString(),
    });
    SpreadsheetApp.getActiveSpreadsheet().toast(
      result.status === "unchanged"
        ? "No data changed; the published dashboard is already current."
        : `Dashboard published as ${result.version_id}.`,
      "Parity Dashboard",
      8,
    );
  } catch (error) {
    SpreadsheetApp.getUi().alert(
      "Dashboard was not published",
      `${error.message}\n\nThe previous published version remains live.`,
      SpreadsheetApp.getUi().ButtonSet.OK,
    );
    throw error;
  } finally {
    lock.releaseLock();
  }
}

function buildDashboardSnapshot_() {
  const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  const control = keyValueTab_(spreadsheet, PARITY_TABS.control, PARITY_HEADERS.control);
  const summary = keyValueTab_(spreadsheet, PARITY_TABS.summary, PARITY_HEADERS.summary);
  const publishedBy = Session.getActiveUser().getEmail() || "Parity Sheet editor";

  return {
    schema_version: PARITY_DASHBOARD_SCHEMA_VERSION,
    source: {
      spreadsheet_id: spreadsheet.getId(),
      spreadsheet_title: spreadsheet.getName(),
      published_at: new Date().toISOString(),
      published_by: publishedBy,
      publish_note: String(control.publish_note || "").trim(),
    },
    assumptions: {
      som_percent: number_(control.som_percent, "som_percent"),
      cad_rate: number_(control.cad_rate, "cad_rate"),
      opt_install: number_(control.opt_install, "opt_install"),
      opt_arr_annual: number_(control.opt_arr_annual, "opt_arr_annual"),
      opt_contract: number_(control.opt_contract, "opt_contract"),
      per_install: number_(control.per_install, "per_install"),
      per_arr_annual: number_(control.per_arr_annual, "per_arr_annual"),
      per_contract: number_(control.per_contract, "per_contract"),
      sync_arr_annual: number_(control.sync_arr_annual, "sync_arr_annual"),
      sync_contract: number_(control.sync_contract, "sync_contract"),
    },
    summary: Object.fromEntries(
      Object.keys(summary).map((key) => [key, number_(summary[key], key)]),
    ),
    metros: tableTab_(spreadsheet, PARITY_TABS.metros, PARITY_HEADERS.metros),
    companies: tableTab_(spreadsheet, PARITY_TABS.companies, PARITY_HEADERS.companies),
    company_regions: tableTab_(
      spreadsheet,
      PARITY_TABS.companyRegions,
      PARITY_HEADERS.companyRegions,
    ),
  };
}

function keyValueTab_(spreadsheet, title, expectedHeaders) {
  const rows = readTab_(spreadsheet, title, expectedHeaders);
  const result = {};
  rows.forEach((row, index) => {
    const key = String(row.key || "").trim();
    if (!key) throw new Error(`${title} row ${index + 2} has a blank key.`);
    if (Object.prototype.hasOwnProperty.call(result, key)) {
      throw new Error(`${title} contains duplicate key "${key}".`);
    }
    result[key] = row.value;
  });
  return result;
}

function tableTab_(spreadsheet, title, expectedHeaders) {
  return readTab_(spreadsheet, title, expectedHeaders).map((row, rowIndex) => {
    const normalized = {};
    expectedHeaders.forEach((header) => {
      const value = row[header];
      normalized[header] = header === "metro" || header === "company"
        ? String(value || "").trim()
        : number_(value, `${title} row ${rowIndex + 2} ${header}`);
    });
    return normalized;
  });
}

function readTab_(spreadsheet, title, expectedHeaders) {
  const sheet = spreadsheet.getSheetByName(title);
  if (!sheet) throw new Error(`Missing required tab "${title}". Run Dashboard → Set up dashboard tabs.`);
  const values = sheet.getDataRange().getValues();
  if (values.length < 2) throw new Error(`${title} has no data rows.`);
  const headers = values[0].map((value) => String(value).trim());
  const missing = expectedHeaders.filter((header) => !headers.includes(header));
  if (missing.length) throw new Error(`${title} is missing columns: ${missing.join(", ")}.`);

  return values.slice(1)
    .filter((row) => row.some((value) => value !== "" && value !== null))
    .map((row) => Object.fromEntries(headers.map((header, index) => [header, row[index]])));
}

function validateSnapshot_(snapshot) {
  const requiredSummary = [
    "total_properties", "optimizer_good_fits", "periscope_good_fits",
    "current_customers", "tam_properties", "sam_properties", "tam_revenue",
    "sam_revenue", "current_revenue", "region_properties",
    "region_opt_fits", "region_per_fits",
  ];
  requiredSummary.forEach((key) => {
    if (!Object.prototype.hasOwnProperty.call(snapshot.summary, key)) {
      throw new Error(`Dashboard Summary is missing key "${key}".`);
    }
  });
  if (snapshot.summary.tam_properties !== snapshot.summary.total_properties) {
    throw new Error("TAM properties must equal total properties.");
  }
  if (
    snapshot.summary.sam_properties !==
    snapshot.summary.region_opt_fits + snapshot.summary.region_per_fits
  ) {
    throw new Error("SAM properties must equal regional Optimizer plus Periscope fits.");
  }
  if (snapshot.assumptions.som_percent < 0 || snapshot.assumptions.som_percent > 100) {
    throw new Error("som_percent must be between 0 and 100.");
  }
  if (snapshot.assumptions.cad_rate <= 0) {
    throw new Error("cad_rate must be greater than zero.");
  }

  uniqueLabels_(snapshot.metros, "metro", "Dashboard Metros");
  uniqueLabels_(snapshot.companies, "company", "Dashboard Companies");
  uniqueLabels_(snapshot.company_regions, "company", "Dashboard Company Regions");

  [snapshot.metros, snapshot.companies, snapshot.company_regions].forEach((rows) => {
    rows.forEach((row) => {
      const components = number_(row.optRev || 0, "optRev") +
        number_(row.perRev || 0, "perRev") +
        number_(row.syncRev || 0, "syncRev");
      if (Math.abs(components - number_(row.totalRev, "totalRev")) > 2) {
        throw new Error(`${row.metro || row.company} totalRev does not equal its revenue components.`);
      }
    });
  });

  const expectedSam =
    snapshot.summary.region_opt_fits *
      (snapshot.assumptions.opt_contract + snapshot.assumptions.sync_contract) +
    snapshot.summary.region_per_fits * snapshot.assumptions.per_contract;
  if (Math.abs(expectedSam - snapshot.summary.sam_revenue) > 2) {
    throw new Error("SAM revenue does not match regional fits and contract assumptions.");
  }
}

function uniqueLabels_(rows, key, tabTitle) {
  const seen = new Set();
  rows.forEach((row, index) => {
    const label = String(row[key] || "").trim();
    if (!label) throw new Error(`${tabTitle} row ${index + 2} has a blank ${key}.`);
    const normalized = label.toLowerCase();
    if (seen.has(normalized)) throw new Error(`${tabTitle} contains duplicate ${key} "${label}".`);
    seen.add(normalized);
  });
}

function number_(value, label) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) {
    throw new Error(`${label} must be a non-negative number.`);
  }
  return number;
}

function hex_(bytes) {
  return bytes.map((value) => {
    const unsigned = (value + 256) % 256;
    return unsigned.toString(16).padStart(2, "0");
  }).join("");
}

function formatDashboardTab_(sheet, columnCount, rowCount) {
  sheet.setFrozenRows(1);
  sheet.getRange(1, 1, 1, columnCount)
    .setBackground("#192027")
    .setFontColor("#FFFFFF")
    .setFontWeight("bold");
  if (rowCount > 1) {
    sheet.getRange(2, 1, rowCount - 1, columnCount)
      .setVerticalAlignment("middle");
  }
  sheet.autoResizeColumns(1, columnCount);
}
