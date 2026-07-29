import { useEffect, useState } from "react";
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, PieChart, Pie, Cell, Treemap, ComposedChart, Line, Area } from "recharts";

const COLORS = {
  opt: "#192027",
  per: "#06B2BC",
  accent: "#06B2BC",
  bg: "#EEEEEE",
  card: "#FDFDFD",
  text: "#192027",
  muted: "#4A5866",
  border: "#DFF2F4",
  highlight: "#DFF2F4",
  current: "#06B2BC",
  sam: "#06B2BC",
  tam: "#192027",
  som: "#4A5866",
  sync: "#4A5866",
};

const fmt = (n) => {
  if (n >= 1e9) return `$${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(0)}K`;
  return `$${n}`;
};

const fmtShort = (n) => {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)}K`;
  return `${n}`;
};

const CustomTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  return (
    <div style={{ background: "#fff", border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: "12px 16px", boxShadow: "0 4px 12px rgba(0,0,0,0.08)", fontFamily: "'DM Sans', sans-serif", fontSize: 13 }}>
      <p style={{ fontWeight: 600, marginBottom: 6, color: COLORS.text }}>{label}</p>
      {payload.map((p, i) => (
        <p key={i} style={{ color: p.color, margin: "2px 0" }}>
          {p.name}: {typeof p.value === "number" && p.value > 10000 ? fmt(p.value) : p.value?.toLocaleString?.() || p.value}
        </p>
      ))}
    </div>
  );
};

const StatCard = ({ label, value, sub, accent }) => (
  <div style={{
    background: accent ? COLORS.opt : COLORS.card,
    borderRadius: 12,
    padding: "24px 28px",
    border: accent ? "none" : `1px solid ${COLORS.border}`,
    flex: 1,
    minWidth: 180,
  }}>
    <div style={{ fontSize: 13, fontWeight: 500, color: accent ? "rgba(255,255,255,0.7)" : COLORS.muted, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 8 }}>{label}</div>
    <div style={{ fontSize: 32, fontWeight: 700, color: accent ? "#fff" : COLORS.text, lineHeight: 1.1, fontFamily: "'Playfair Display', serif" }}>{value}</div>
    {sub && <div style={{ fontSize: 13, color: accent ? "rgba(255,255,255,0.6)" : COLORS.muted, marginTop: 6 }}>{sub}</div>}
  </div>
);

const SectionTitle = ({ title, subtitle }) => (
  <div style={{ marginBottom: 24, marginTop: 48 }}>
    <h2 style={{ fontSize: 22, fontWeight: 700, color: COLORS.text, margin: 0, fontFamily: "'Playfair Display', serif" }}>{title}</h2>
    {subtitle && <p style={{ fontSize: 14, color: COLORS.muted, margin: "6px 0 0" }}>{subtitle}</p>}
  </div>
);

const tabs = ["Overview", "Revenue", "By Metro", "By Company"];

const NoPublishedData = ({ message }) => (
  <div style={{
    minHeight: "100vh",
    display: "grid",
    placeItems: "center",
    padding: 24,
    background: COLORS.bg,
    fontFamily: "'DM Sans', sans-serif",
  }}>
    <div style={{
      width: "min(100%, 560px)",
      padding: 36,
      border: `1px solid ${COLORS.border}`,
      borderRadius: 14,
      background: COLORS.card,
      textAlign: "center",
    }}>
      <div style={{ color: COLORS.accent, fontSize: 12, fontWeight: 800, letterSpacing: "0.1em", textTransform: "uppercase" }}>
        Parity
      </div>
      <h1 style={{ margin: "12px 0 10px", color: COLORS.text, fontFamily: "'Playfair Display', serif" }}>
        Market Runway Dashboard
      </h1>
      <p style={{ margin: 0, color: COLORS.muted, lineHeight: 1.6 }}>{message}</p>
    </div>
  </div>
);

export default function Dashboard() {
  const [activeTab, setActiveTab] = useState("Overview");
  const [somPercent, setSomPercent] = useState(50);
  const [currency, setCurrency] = useState("USD");
  const [publishedSnapshot, setPublishedSnapshot] = useState(null);
  const [publishedVersion, setPublishedVersion] = useState(null);
  const [dataStatus, setDataStatus] = useState("Loading published data…");

  useEffect(() => {
    const controller = new AbortController();
    fetch("/market-runway/data", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    })
      .then((response) => {
        if (!response.ok) throw new Error(`Dashboard data returned ${response.status}`);
        return response.json();
      })
      .then((payload) => {
        if (payload?.data) {
          setPublishedSnapshot(payload.data);
          setPublishedVersion(payload.version || null);
          setSomPercent(payload.data.assumptions?.som_percent ?? 50);
          setDataStatus("Published from Google Sheets");
        } else {
          setDataStatus("No dashboard data has been published from Google Sheets yet.");
        }
      })
      .catch((error) => {
        if (error.name !== "AbortError") {
          setDataStatus("Published dashboard data is temporarily unavailable. Please try again.");
        }
      });
    return () => controller.abort();
  }, []);

  if (!publishedSnapshot) {
    return <NoPublishedData message={dataStatus} />;
  }

  const summary = publishedSnapshot.summary;
  const assumptions = publishedSnapshot.assumptions;
  const metroData = publishedSnapshot.metros;
  const companyData = publishedSnapshot.companies;
  const companyRegionData = publishedSnapshot.company_regions;

  const CAD_RATE = assumptions.cad_rate;
  const conv = (v) => currency === "CAD" ? v * CAD_RATE : v;
  const sym = currency === "CAD" ? "C$" : "$";
  const fmt = (n) => {
    const val = conv(n);
    if (Math.abs(val) >= 1e9) return `${sym}${(val / 1e9).toFixed(1)}B`;
    if (Math.abs(val) >= 1e6) return `${sym}${(val / 1e6).toFixed(1)}M`;
    if (Math.abs(val) >= 1e3) {
      const k = val / 1e3;
      return `${sym}${Number.isInteger(k) ? k : k.toFixed(1)}K`;
    }
    return `${sym}${val}`;
  };

  const somRevenue = Math.round(summary.sam_revenue * somPercent / 100);
  const somProperties = Math.round(summary.sam_properties * somPercent / 100);

  const tamSamSom = [
    { name: "TAM", value: summary.tam_revenue, properties: summary.tam_properties, label: "Top 12 Clients TAM" },
    { name: "SAM", value: summary.sam_revenue, properties: summary.sam_properties, label: "Serviceable Addressable Market" },
    { name: "SOM", value: somRevenue, properties: somProperties, label: "Serviceable Obtainable Market" },
  ];
  return (
    <div style={{ fontFamily: "'DM Sans', sans-serif", background: COLORS.bg, minHeight: "100vh", color: COLORS.text }}>
      <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Playfair+Display:wght@600;700;800&display=swap" rel="stylesheet" />

      {/* Header */}
      <div style={{ background: COLORS.opt, padding: "40px 48px 24px", borderBottom: `3px solid ${COLORS.accent}` }}>
        <div style={{ maxWidth: 1200, margin: "0 auto", display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 24, flexWrap: "wrap" }}>
          <div style={{ flex: 1, minWidth: 280 }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: COLORS.accent, textTransform: "uppercase", letterSpacing: "0.15em", marginBottom: 8 }}>Existing Portfolio Analysis</div>
            <h1 style={{ fontSize: 36, fontWeight: 800, color: "#fff", margin: 0, fontFamily: "'Playfair Display', serif" }}>Runway With Top 12 Clients</h1>
            <p style={{ fontSize: 15, color: "rgba(255,255,255,0.6)", margin: "8px 0 0" }}>Optimizer: {fmt(assumptions.opt_install)} install + {fmt(assumptions.opt_arr_annual)}/yr ARR · Periscope: {fmt(assumptions.per_install)} install + {fmt(assumptions.per_arr_annual)}/yr ARR · Sync: {fmt(assumptions.sync_arr_annual)}/yr ARR (Optimizer bldgs only) · 5-year contracts · SAM = good fits in our regions</p>
          </div>
          <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 6 }}>
            <div style={{ display: "flex", background: "rgba(255,255,255,0.08)", borderRadius: 8, padding: 4, border: "1px solid rgba(255,255,255,0.15)" }}>
              <button
                onClick={() => setCurrency("USD")}
                style={{
                  padding: "8px 16px", border: "none", borderRadius: 6,
                  background: currency === "USD" ? COLORS.accent : "transparent",
                  color: currency === "USD" ? COLORS.opt : "rgba(255,255,255,0.7)",
                  fontSize: 13, fontWeight: 700, cursor: "pointer",
                  fontFamily: "'DM Sans', sans-serif", transition: "all 0.15s"
                }}
              >USD</button>
              <button
                onClick={() => setCurrency("CAD")}
                style={{
                  padding: "8px 16px", border: "none", borderRadius: 6,
                  background: currency === "CAD" ? COLORS.accent : "transparent",
                  color: currency === "CAD" ? COLORS.opt : "rgba(255,255,255,0.7)",
                  fontSize: 13, fontWeight: 700, cursor: "pointer",
                  fontFamily: "'DM Sans', sans-serif", transition: "all 0.15s"
                }}
              >CAD</button>
            </div>
            <div style={{ fontSize: 11, color: "rgba(255,255,255,0.5)" }}>1 USD = {CAD_RATE} CAD</div>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div style={{ background: "#fff", borderBottom: `1px solid ${COLORS.border}`, position: "sticky", top: 0, zIndex: 10 }}>
        <div style={{ maxWidth: 1200, margin: "0 auto", display: "flex", gap: 0, padding: "0 48px" }}>
          {tabs.map(t => (
            <button key={t} onClick={() => setActiveTab(t)} style={{
              padding: "16px 24px", border: "none", background: "none", cursor: "pointer",
              fontSize: 14, fontWeight: activeTab === t ? 700 : 500,
              color: activeTab === t ? COLORS.opt : COLORS.muted,
              borderBottom: activeTab === t ? `3px solid ${COLORS.opt}` : "3px solid transparent",
              fontFamily: "'DM Sans', sans-serif", transition: "all 0.2s"
            }}>{t}</button>
          ))}
        </div>
      </div>

      <div style={{ maxWidth: 1200, margin: "0 auto", padding: "32px 48px 80px" }}>
        <div style={{
          background: "#fff",
          border: `1px solid ${COLORS.border}`,
          borderRadius: 10,
          padding: "12px 16px",
          color: COLORS.muted,
          fontSize: 12,
          display: "flex",
          justifyContent: "space-between",
          gap: 12,
          flexWrap: "wrap",
        }}>
          <span><strong style={{ color: COLORS.text }}>Parity Internal &amp; Confidential</strong> · {dataStatus}</span>
          <span>
            {publishedVersion?.published_at
              ? `Last published ${new Date(publishedVersion.published_at).toLocaleString()}`
              : "Not yet connected to the working Sheet"}
          </span>
        </div>

        {/* OVERVIEW TAB */}
        {activeTab === "Overview" && (
          <>
            {/* KPI Cards */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
              <StatCard accent label="Total Properties" value={summary.total_properties.toLocaleString()} />
              <StatCard label="Current Customers" value={summary.current_customers.toLocaleString()} />
              <StatCard label="Optimizer + Sync Fits" value={summary.optimizer_good_fits.toLocaleString()} sub={`${fmt(assumptions.opt_contract + assumptions.sync_contract)}/bldg = ${fmt(summary.optimizer_good_fits * (assumptions.opt_contract + assumptions.sync_contract))}`} />
              <StatCard label="Periscope Fits" value={summary.periscope_good_fits.toLocaleString()} sub={`${fmt(assumptions.per_contract)}/bldg = ${fmt(summary.periscope_good_fits * assumptions.per_contract)}`} />
            </div>

            <SectionTitle title="Properties in Our Regions" subtitle="NY · NJ · Greater Boston · Greater Seattle · Toronto · California · DMV" />
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 16, marginBottom: 16 }}>
              <StatCard label="Total in Regions" value={summary.region_properties.toLocaleString()} sub={`${(summary.region_properties / summary.total_properties * 100).toFixed(1)}% of portfolio`} />
              <StatCard label="Opt + Sync Fits in Regions" value={summary.region_opt_fits.toLocaleString()} sub={`${(summary.region_opt_fits / summary.optimizer_good_fits * 100).toFixed(1)}% of all Optimizer fits`} />
              <StatCard label="Periscope Fits in Regions" value={summary.region_per_fits.toLocaleString()} sub={`${(summary.region_per_fits / summary.periscope_good_fits * 100).toFixed(1)}% of all Periscope fits`} />
            </div>

            {/* TAM / SAM / SOM */}
            <SectionTitle title="Market Sizing" subtitle={`Top 12 Clients TAM = all properties · SAM = good fits in our regions · SOM = ${somPercent}% of SAM`} />
            <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
              <div style={{ flex: 2, minWidth: 400, background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 32 }}>
                <div style={{ display: "flex", justifyContent: "center", alignItems: "center", height: 320 }}>
                  {/* Concentric circles */}
                  <svg viewBox="0 0 500 380" width="100%" height="100%">
                    {/* TAM */}
                    <ellipse cx="250" cy="200" rx="230" ry="170" fill={COLORS.tam} opacity="0.12" stroke={COLORS.tam} strokeWidth="2" />
                    <text x="250" y="42" textAnchor="middle" fontSize="13" fontWeight="700" fill={COLORS.tam}>Top 12 Clients TAM</text>
                    <text x="250" y="58" textAnchor="middle" fontSize="12" fill={COLORS.muted}>{fmt(summary.tam_revenue)} · {summary.tam_properties.toLocaleString()} properties</text>
                    {/* SAM */}
                    <ellipse cx="250" cy="215" rx="165" ry="120" fill={COLORS.sam} opacity="0.15" stroke={COLORS.sam} strokeWidth="2" />
                    <text x="250" y="112" textAnchor="middle" fontSize="13" fontWeight="700" fill={COLORS.sam}>SAM</text>
                    <text x="250" y="128" textAnchor="middle" fontSize="12" fill={COLORS.muted}>{fmt(summary.sam_revenue)} · {summary.sam_properties.toLocaleString()} fits</text>
                    {/* SOM */}
                    <ellipse cx="250" cy="240" rx="100" ry="72" fill={COLORS.som} opacity="0.2" stroke={COLORS.som} strokeWidth="2" />
                    <text x="250" y="220" textAnchor="middle" fontSize="13" fontWeight="700" fill={COLORS.opt}>SOM ({somPercent}%)</text>
                    <text x="250" y="240" textAnchor="middle" fontSize="12" fill={COLORS.muted}>{fmt(somRevenue)}</text>
                  </svg>
                </div>
              </div>
              <div style={{ flex: 1, minWidth: 250, display: "flex", flexDirection: "column", gap: 12 }}>
                {tamSamSom.map((d, i) => (
                  <div key={d.name} style={{ background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: "20px 24px" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <div>
                        <div style={{ fontSize: 12, color: COLORS.muted, fontWeight: 500, textTransform: "uppercase", letterSpacing: "0.05em" }}>{d.label}</div>
                        <div style={{ fontSize: 26, fontWeight: 700, fontFamily: "'Playfair Display', serif", color: COLORS.opt }}>{fmt(d.value)}</div>
                      </div>
                      <div style={{ fontSize: 13, color: COLORS.muted, textAlign: "right" }}>
                        <div style={{ fontWeight: 600, color: COLORS.text }}>{d.properties.toLocaleString()}</div>
                        <div>properties</div>
                      </div>
                    </div>
                  </div>
                ))}
                <div style={{ background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: "16px 24px" }}>
                  <div style={{ fontSize: 12, color: COLORS.muted, fontWeight: 500, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 10 }}>Adjust SOM %</div>
                  <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <input
                      type="range"
                      min="10"
                      max="100"
                      step="5"
                      value={somPercent}
                      onChange={(e) => setSomPercent(Number(e.target.value))}
                      style={{ flex: 1, accentColor: COLORS.per, cursor: "pointer" }}
                    />
                    <span style={{ fontSize: 22, fontWeight: 800, color: COLORS.opt, fontFamily: "'Playfair Display', serif", minWidth: 52, textAlign: "right" }}>{somPercent}%</span>
                  </div>
                </div>
              </div>
            </div>

            {/* Service Split */}
            <SectionTitle title="Good Fits by Service" subtitle={`Breakdown of qualified properties across Optimizer (${fmt(assumptions.opt_contract)}), Periscope (${fmt(assumptions.per_contract)}), and Sync (${fmt(assumptions.sync_contract)}) · 5-year contract`} />
            <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
              <div style={{ flex: 1, minWidth: 300, background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 32 }}>
                <ResponsiveContainer width="100%" height={300}>
                  <PieChart>
                    <Pie data={[
                      { name: "Optimizer + Sync Fits", value: summary.optimizer_good_fits },
                      { name: "Periscope Fits", value: summary.periscope_good_fits },
                      { name: "Not a Fit", value: summary.total_properties - summary.optimizer_good_fits - summary.periscope_good_fits },
                    ]} cx="50%" cy="50%" outerRadius={110} innerRadius={60} dataKey="value" paddingAngle={2}>
                      <Cell fill={COLORS.opt} />
                      <Cell fill={COLORS.per} />
                      <Cell fill="#EEEEEE" />
                    </Pie>
                    <Tooltip content={<CustomTooltip />} />
                    <Legend />
                  </PieChart>
                </ResponsiveContainer>
              </div>
              <div style={{ flex: 1, minWidth: 300, background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 32 }}>
                <ResponsiveContainer width="100%" height={300}>
                  <PieChart>
                    <Pie data={[
                      { name: "Optimizer Revenue", value: summary.optimizer_good_fits * assumptions.opt_contract },
                      { name: "Periscope Revenue", value: summary.periscope_good_fits * assumptions.per_contract },
                      { name: "Sync Revenue", value: summary.optimizer_good_fits * assumptions.sync_contract },
                    ]} cx="50%" cy="50%" outerRadius={110} innerRadius={60} dataKey="value" paddingAngle={2}>
                      <Cell fill={COLORS.opt} />
                      <Cell fill={COLORS.per} />
                      <Cell fill={COLORS.sync} />
                    </Pie>
                    <Tooltip content={<CustomTooltip />} />
                    <Legend formatter={(v) => `${v}`} />
                  </PieChart>
                </ResponsiveContainer>
                <div style={{ textAlign: "center", fontSize: 13, color: COLORS.muted, marginTop: 8 }}>
                  Opt: {fmt(summary.optimizer_good_fits * assumptions.opt_contract)} · Per: {fmt(summary.periscope_good_fits * assumptions.per_contract)} · Sync: {fmt(summary.optimizer_good_fits * assumptions.sync_contract)}
                </div>
              </div>
            </div>
          </>
        )}

        {/* BY METRO TAB */}
        {activeTab === "By Metro" && (
          <>
            <SectionTitle title="Good Fits by Metro Area" subtitle="Top 15 metro areas ranked by total good fits (Optimizer + Periscope)" />
            <div style={{ background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 32 }}>
              <ResponsiveContainer width="100%" height={500}>
                <BarChart data={[...metroData].sort((a, b) => (b.opt + b.per) - (a.opt + a.per))} layout="vertical" margin={{ left: 20, right: 30 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#DFF2F4" />
                  <XAxis type="number" tickFormatter={v => v} fontSize={12} />
                  <YAxis type="category" dataKey="metro" width={120} fontSize={12} tick={{ fill: COLORS.text }} />
                  <Tooltip content={({ active, payload, label }) => {
                    if (!active || !payload?.length) return null;
                    const total = payload.reduce((s, p) => s + (p.value || 0), 0);
                    return (
                      <div style={{ background: "#fff", border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: "12px 16px", boxShadow: "0 4px 12px rgba(0,0,0,0.08)", fontFamily: "'DM Sans', sans-serif", fontSize: 13 }}>
                        <p style={{ fontWeight: 600, marginBottom: 6, color: COLORS.text }}>{label}</p>
                        {payload.map((p, i) => (
                          <p key={i} style={{ color: p.color, margin: "2px 0" }}>{p.name}: {p.value?.toLocaleString()}</p>
                        ))}
                        <p style={{ fontWeight: 700, color: COLORS.text, margin: "6px 0 0", borderTop: `1px solid ${COLORS.border}`, paddingTop: 6 }}>Total: {total.toLocaleString()}</p>
                      </div>
                    );
                  }} />
                  <Legend />
                  <Bar dataKey="opt" name="Optimizer Fits" fill={COLORS.opt} radius={[0, 4, 4, 0]} stackId="a" />
                  <Bar dataKey="per" name="Periscope Fits" fill={COLORS.per} radius={[0, 4, 4, 0]} stackId="a" />
                </BarChart>
              </ResponsiveContainer>
            </div>

            <SectionTitle title="Revenue Opportunity by Metro" subtitle={`5-year contract value: Optimizer ${fmt(assumptions.opt_contract)}/bldg · Periscope ${fmt(assumptions.per_contract)}/bldg`} />
            <div style={{ background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 32 }}>
              <ResponsiveContainer width="100%" height={500}>
                <BarChart data={[...metroData].sort((a, b) => b.totalRev - a.totalRev)} layout="vertical" margin={{ left: 20, right: 30 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#DFF2F4" />
                  <XAxis type="number" tickFormatter={fmt} fontSize={12} />
                  <YAxis type="category" dataKey="metro" width={120} fontSize={12} tick={{ fill: COLORS.text }} />
                  <Tooltip content={({ active, payload, label }) => {
                    if (!active || !payload?.length) return null;
                    const total = payload.reduce((s, p) => s + (p.value || 0), 0);
                    return (
                      <div style={{ background: "#fff", border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: "12px 16px", boxShadow: "0 4px 12px rgba(0,0,0,0.08)", fontFamily: "'DM Sans', sans-serif", fontSize: 13 }}>
                        <p style={{ fontWeight: 600, marginBottom: 6, color: COLORS.text }}>{label}</p>
                        {payload.map((p, i) => (
                          <p key={i} style={{ color: p.color, margin: "2px 0" }}>{p.name}: {fmt(p.value)}</p>
                        ))}
                        <p style={{ fontWeight: 700, color: COLORS.text, margin: "6px 0 0", borderTop: `1px solid ${COLORS.border}`, paddingTop: 6 }}>Total: {fmt(total)}</p>
                      </div>
                    );
                  }} />
                  <Legend />
                  <Bar dataKey="optRev" name="Optimizer Revenue" fill={COLORS.opt} radius={[0, 0, 0, 0]} stackId="a" />
                  <Bar dataKey="perRev" name="Periscope Revenue" fill={COLORS.per} radius={[0, 0, 0, 0]} stackId="a" />
                  <Bar dataKey="syncRev" name="Sync Revenue" fill={COLORS.sync} radius={[0, 4, 4, 0]} stackId="a" />
                </BarChart>
              </ResponsiveContainer>
            </div>

            <SectionTitle title="Metro Concentration" subtitle="Block size represents total revenue opportunity by metro" />
            <div style={{ background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 32 }}>
              <ResponsiveContainer width="100%" height={400}>
                <Treemap
                  data={metroData.map(d => ({ name: d.metro, size: d.totalRev, opt: d.opt, per: d.per }))}
                  dataKey="size"
                  aspectRatio={4 / 3}
                  stroke="#FDFDFD"
                  content={({ x, y, width, height, name, size }) => {
                    if (width < 30 || height < 25) return null;
                    const idx = metroData.findIndex(d => d.metro === name);
                    const opacity = 0.7 + (0.3 * (1 - idx / metroData.length));
                    const fontSize = width > 100 ? 13 : width > 60 ? 10 : 8;
                    return (
                      <g>
                        <rect x={x} y={y} width={width} height={height} fill={COLORS.opt} fillOpacity={opacity} stroke="#FDFDFD" strokeWidth={2} rx={4} />
                        <text x={x + width / 2} y={y + height / 2 - (height > 35 ? 8 : 0)} textAnchor="middle" fill="#06B2BC" fillOpacity="1" stroke="none" fontSize={fontSize} fontWeight="800">{name}</text>
                        {height > 35 && (
                          <text x={x + width / 2} y={y + height / 2 + 10} textAnchor="middle" fill="#FDFDFD" fillOpacity="1" stroke="none" fontSize={fontSize - 1}>{fmt(size)}</text>
                        )}
                      </g>
                    );
                  }}
                />
              </ResponsiveContainer>
            </div>
          </>
        )}

        {/* BY COMPANY TAB */}
        {activeTab === "By Company" && (
          <>
            <SectionTitle title="Service Fit by Company" subtitle="Number of good fit properties for each service across all portfolio companies" />
            <div style={{ background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 32 }}>
              <ResponsiveContainer width="100%" height={480}>
                <BarChart data={[...companyData].sort((a, b) => (b.opt + b.per) - (a.opt + a.per))} layout="vertical" margin={{ left: 10, right: 30 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#DFF2F4" />
                  <XAxis type="number" fontSize={12} />
                  <YAxis type="category" dataKey="company" width={120} fontSize={12} tick={{ fill: COLORS.text }} />
                  <Tooltip content={({ active, payload, label }) => {
                    if (!active || !payload?.length) return null;
                    const total = payload.reduce((s, p) => s + (p.value || 0), 0);
                    return (
                      <div style={{ background: "#fff", border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: "12px 16px", boxShadow: "0 4px 12px rgba(0,0,0,0.08)", fontFamily: "'DM Sans', sans-serif", fontSize: 13 }}>
                        <p style={{ fontWeight: 600, marginBottom: 6, color: COLORS.text }}>{label}</p>
                        {payload.map((p, i) => (
                          <p key={i} style={{ color: p.color, margin: "2px 0" }}>{p.name}: {p.value?.toLocaleString()}</p>
                        ))}
                        <p style={{ fontWeight: 700, color: COLORS.text, margin: "6px 0 0", borderTop: `1px solid ${COLORS.border}`, paddingTop: 6 }}>Total: {total.toLocaleString()}</p>
                      </div>
                    );
                  }} />
                  <Legend />
                  <Bar dataKey="opt" name="Optimizer Fits" fill={COLORS.opt} radius={[0, 4, 4, 0]} stackId="a" />
                  <Bar dataKey="per" name="Periscope Fits" fill={COLORS.per} radius={[0, 4, 4, 0]} stackId="a" />
                </BarChart>
              </ResponsiveContainer>
            </div>

            <SectionTitle title="Service Fit by Company — In Our Regions" subtitle="Good fits in NY · NJ · Greater Boston · Greater Seattle · California · DMV" />
            <div style={{ background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 32 }}>
              <ResponsiveContainer width="100%" height={480}>
                <BarChart data={[...companyRegionData].sort((a, b) => (b.opt + b.per) - (a.opt + a.per))} layout="vertical" margin={{ left: 10, right: 30 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#DFF2F4" />
                  <XAxis type="number" fontSize={12} />
                  <YAxis type="category" dataKey="company" width={120} fontSize={12} tick={{ fill: COLORS.text }} />
                  <Tooltip content={({ active, payload, label }) => {
                    if (!active || !payload?.length) return null;
                    const total = payload.reduce((s, p) => s + (p.value || 0), 0);
                    return (
                      <div style={{ background: "#fff", border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: "12px 16px", boxShadow: "0 4px 12px rgba(0,0,0,0.08)", fontFamily: "'DM Sans', sans-serif", fontSize: 13 }}>
                        <p style={{ fontWeight: 600, marginBottom: 6, color: COLORS.text }}>{label}</p>
                        {payload.map((p, i) => (
                          <p key={i} style={{ color: p.color, margin: "2px 0" }}>{p.name}: {p.value?.toLocaleString()}</p>
                        ))}
                        <p style={{ fontWeight: 700, color: COLORS.text, margin: "6px 0 0", borderTop: `1px solid ${COLORS.border}`, paddingTop: 6 }}>Total: {total.toLocaleString()}</p>
                      </div>
                    );
                  }} />
                  <Legend />
                  <Bar dataKey="opt" name="Optimizer Fits" fill={COLORS.opt} radius={[0, 4, 4, 0]} stackId="a" />
                  <Bar dataKey="per" name="Periscope Fits" fill={COLORS.per} radius={[0, 4, 4, 0]} stackId="a" />
                </BarChart>
              </ResponsiveContainer>
            </div>

            <SectionTitle title="Revenue Opportunity by Company" subtitle="5-year contract value for good fits in our regions · Optimizer vs Periscope" />
            <div style={{ background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 32 }}>
              <ResponsiveContainer width="100%" height={480}>
                <BarChart data={companyRegionData} layout="vertical" margin={{ left: 10, right: 30 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#DFF2F4" />
                  <XAxis type="number" tickFormatter={fmt} fontSize={12} />
                  <YAxis type="category" dataKey="company" width={120} fontSize={12} tick={{ fill: COLORS.text }} />
                  <Tooltip content={<CustomTooltip />} />
                  <Legend />
                  <Bar dataKey="optRev" name="Optimizer Revenue" fill={COLORS.opt} radius={[0, 0, 0, 0]} stackId="a" />
                  <Bar dataKey="perRev" name="Periscope Revenue" fill={COLORS.per} radius={[0, 0, 0, 0]} stackId="a" />
                  <Bar dataKey="syncRev" name="Sync Revenue" fill={COLORS.sync} radius={[0, 4, 4, 0]} stackId="a" />
                </BarChart>
              </ResponsiveContainer>
            </div>

            <SectionTitle title="Customer Penetration by Company" subtitle="Percentage of Optimizer good fits that are already customers" />
            <div style={{ background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 32 }}>
              <ResponsiveContainer width="100%" height={480}>
                <ComposedChart data={[...companyData].sort((a, b) => b.penetration - a.penetration)} layout="vertical" margin={{ left: 10, right: 30 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#DFF2F4" />
                  <XAxis type="number" tickFormatter={v => `${v}%`} fontSize={12} />
                  <YAxis type="category" dataKey="company" width={120} fontSize={12} tick={{ fill: COLORS.text }} />
                  <Tooltip content={({ active, payload, label }) => {
                    if (!active || !payload?.length) return null;
                    return (
                      <div style={{ background: "#fff", border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: "12px 16px", boxShadow: "0 4px 12px rgba(0,0,0,0.08)", fontSize: 13 }}>
                        <p style={{ fontWeight: 600, marginBottom: 4 }}>{label}</p>
                        <p>Penetration: {payload[0]?.value}%</p>
                      </div>
                    );
                  }} />
                  <Bar dataKey="penetration" name="Penetration %" fill={COLORS.accent} radius={[0, 6, 6, 0]} barSize={20} />
                </ComposedChart>
              </ResponsiveContainer>
            </div>

            <SectionTitle title="Top 12 Clients Overview" subtitle="Good fits in our regions · NY · NJ · Greater Boston · Greater Seattle · California · DMV" />
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "separate", borderSpacing: 0, background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, overflow: "hidden", fontSize: 13 }}>
                <thead>
                  <tr style={{ background: COLORS.highlight }}>
                    {["Company", "Total Bldgs", "Optimizer Fits", "Periscope Fits", "Contracted Buildings", "Current Penetration", "Remaining One-Time Rev Opp.", "Remaining ARR Rev Opp."].map(h => (
                      <th key={h} style={{ padding: "14px 16px", textAlign: "left", fontWeight: 600, color: COLORS.text, borderBottom: `1px solid ${COLORS.border}` }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {companyRegionData.map((r, i) => {
                    const optRemaining = Math.max(r.opt - r.cust, 0);
                    const perRemaining = r.per;
                    const remainingOneTime = optRemaining * assumptions.opt_install + perRemaining * assumptions.per_install;
                    const remainingArrAnnual = optRemaining * assumptions.opt_arr_annual + perRemaining * assumptions.per_arr_annual + optRemaining * assumptions.sync_arr_annual;
                    return (
                      <tr key={r.company} style={{ background: i % 2 === 0 ? "#fff" : COLORS.bg }}>
                        <td style={{ padding: "12px 16px", fontWeight: 600 }}>{r.company}</td>
                        <td style={{ padding: "12px 16px" }}>{r.total.toLocaleString()}</td>
                        <td style={{ padding: "12px 16px", color: COLORS.opt, fontWeight: 600 }}>{r.opt}</td>
                        <td style={{ padding: "12px 16px", color: COLORS.per, fontWeight: 600 }}>{r.per}</td>
                        <td style={{ padding: "12px 16px" }}>{r.cust}</td>
                        <td style={{ padding: "12px 16px" }}>{r.penetration}%</td>
                        <td style={{ padding: "12px 16px", fontWeight: 600 }}>{fmt(remainingOneTime)}</td>
                        <td style={{ padding: "12px 16px", fontWeight: 600 }}>{fmt(remainingArrAnnual)}/yr</td>
                      </tr>
                    );
                  })}
                  <tr style={{ background: COLORS.opt, color: "#fff", fontWeight: 700 }}>
                    <td style={{ padding: "12px 16px" }}>TOTAL</td>
                    <td style={{ padding: "12px 16px" }}>{companyRegionData.reduce((s, r) => s + r.total, 0).toLocaleString()}</td>
                    <td style={{ padding: "12px 16px" }}>{companyRegionData.reduce((s, r) => s + r.opt, 0)}</td>
                    <td style={{ padding: "12px 16px" }}>{companyRegionData.reduce((s, r) => s + r.per, 0)}</td>
                    <td style={{ padding: "12px 16px" }}>{companyRegionData.reduce((s, r) => s + r.cust, 0)}</td>
                    <td style={{ padding: "12px 16px" }}>—</td>
                    <td style={{ padding: "12px 16px" }}>{fmt(companyRegionData.reduce((s, r) => s + Math.max(r.opt - r.cust, 0) * assumptions.opt_install + r.per * assumptions.per_install, 0))}</td>
                    <td style={{ padding: "12px 16px" }}>{fmt(companyRegionData.reduce((s, r) => s + Math.max(r.opt - r.cust, 0) * (assumptions.opt_arr_annual + assumptions.sync_arr_annual) + r.per * assumptions.per_arr_annual, 0))}/yr</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <SectionTitle title="Customer Roadmap by Service" subtitle="Expansion opportunity within each existing portfolio company — Optimizer + Sync and Periscope fits remaining" />

            {companyData.map(d => {
              const optRemaining = d.opt - d.cust;
              const optTotal = d.opt;
              const perTotal = d.per;
              const maxBar = Math.max(optTotal, perTotal, 1);
              return (
                <div key={d.company} style={{ background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: "24px 28px", marginBottom: 16 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
                    <div>
                      <span style={{ fontSize: 16, fontWeight: 700, color: COLORS.text }}>{d.company}</span>
                      <span style={{ fontSize: 13, color: COLORS.muted, marginLeft: 12 }}>{d.total.toLocaleString()} properties</span>
                    </div>
                    <div style={{ fontSize: 18, fontWeight: 700, color: COLORS.opt, fontFamily: "'Playfair Display', serif" }}>{fmt(d.totalRev)} <span style={{ fontSize: 12, fontWeight: 400, color: COLORS.muted }}>potential</span></div>
                  </div>
                  {/* Optimizer bar */}
                  <div style={{ marginBottom: 10 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
                      <span style={{ color: COLORS.opt, fontWeight: 600 }}>Optimizer + Sync — {optTotal} fits</span>
                      <span style={{ color: COLORS.muted }}>{d.cust} customers · {optRemaining > 0 ? optRemaining : 0} remaining</span>
                    </div>
                    <div style={{ background: COLORS.bg, borderRadius: 6, height: 24, position: "relative", overflow: "hidden" }}>
                      {d.cust > 0 && (
                        <div style={{ position: "absolute", left: 0, top: 0, height: "100%", width: `${(d.cust / Math.max(maxBar, 1)) * 100}%`, background: COLORS.accent, borderRadius: "6px 0 0 6px", zIndex: 2 }} />
                      )}
                      <div style={{ position: "absolute", left: 0, top: 0, height: "100%", width: `${(optTotal / Math.max(maxBar, 1)) * 100}%`, background: COLORS.opt, borderRadius: 6, opacity: 0.3, zIndex: 1 }} />
                    </div>
                  </div>
                  {/* Periscope bar */}
                  <div>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
                      <span style={{ color: COLORS.per, fontWeight: 600 }}>Periscope — {perTotal} fits</span>
                      <span style={{ color: COLORS.muted }}>{perTotal} available</span>
                    </div>
                    <div style={{ background: COLORS.bg, borderRadius: 6, height: 24, position: "relative", overflow: "hidden" }}>
                      <div style={{ position: "absolute", left: 0, top: 0, height: "100%", width: `${(perTotal / Math.max(maxBar, 1)) * 100}%`, background: COLORS.per, borderRadius: 6, opacity: 0.3 }} />
                    </div>
                  </div>
                </div>
              );
            })}

            <SectionTitle title="Expansion Priority Matrix" subtitle="Companies ranked by total opportunity vs current penetration — bigger bubbles = more revenue opportunity" />
            <div style={{ background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 32 }}>
              <ResponsiveContainer width="100%" height={400}>
                <ComposedChart data={companyData.filter(d => d.opt + d.per > 0)} margin={{ left: 20, right: 30, bottom: 20 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#DFF2F4" />
                  <XAxis dataKey="company" fontSize={11} angle={-30} textAnchor="end" height={60} tick={{ fill: COLORS.text }} />
                  <YAxis yAxisId="left" tickFormatter={fmt} fontSize={12} label={{ value: "Revenue Opportunity", angle: -90, position: "insideLeft", fill: COLORS.muted, fontSize: 12 }} />
                  <YAxis yAxisId="right" orientation="right" tickFormatter={v => `${v}%`} fontSize={12} label={{ value: "Penetration %", angle: 90, position: "insideRight", fill: COLORS.muted, fontSize: 12 }} />
                  <Tooltip content={<CustomTooltip />} />
                  <Legend />
                  <Bar yAxisId="left" dataKey="totalRev" name="Total Revenue Opp." fill={COLORS.opt} radius={[4, 4, 0, 0]} opacity={0.7} />
                  <Line yAxisId="right" dataKey="penetration" name="Penetration %" stroke={COLORS.accent} strokeWidth={3} dot={{ r: 5, fill: COLORS.accent }} />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          </>
        )}

        {/* REVENUE TAB */}
        {activeTab === "Revenue" && (
          <>
            <SectionTitle title="Revenue Breakdown per Building" subtitle="5-year average contract length · one-time install + annual recurring revenue" />
            <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginBottom: 40 }}>
              <div style={{ flex: 1, minWidth: 260, background: COLORS.card, borderRadius: 12, border: `2px solid ${COLORS.opt}`, padding: 32, textAlign: "center" }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: COLORS.opt, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 16 }}>Optimizer</div>
                <div style={{ display: "flex", justifyContent: "center", gap: 24, marginBottom: 16 }}>
                  <div>
                    <div style={{ fontSize: 13, color: COLORS.muted, marginBottom: 4 }}>Install</div>
                    <div style={{ fontSize: 28, fontWeight: 800, color: COLORS.opt, fontFamily: "'Playfair Display', serif" }}>{fmt(assumptions.opt_install)}</div>
                  </div>
                  <div style={{ borderLeft: `1px solid ${COLORS.border}`, paddingLeft: 24 }}>
                    <div style={{ fontSize: 13, color: COLORS.muted, marginBottom: 4 }}>ARR</div>
                    <div style={{ fontSize: 28, fontWeight: 800, color: COLORS.opt, fontFamily: "'Playfair Display', serif" }}>{fmt(assumptions.opt_arr_annual)}</div>
                  </div>
                </div>
                <div style={{ background: COLORS.bg, borderRadius: 8, padding: "10px 16px", marginBottom: 12 }}>
                  <div style={{ fontSize: 13, color: COLORS.muted }}>5-Year Contract Value</div>
                  <div style={{ fontSize: 22, fontWeight: 700, color: COLORS.opt, fontFamily: "'Playfair Display', serif" }}>{fmt(assumptions.opt_contract)}</div>
                </div>
              </div>
              <div style={{ flex: 1, minWidth: 260, background: COLORS.card, borderRadius: 12, border: `2px solid ${COLORS.per}`, padding: 32, textAlign: "center" }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: COLORS.per, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 16 }}>Periscope</div>
                <div style={{ display: "flex", justifyContent: "center", gap: 24, marginBottom: 16 }}>
                  <div>
                    <div style={{ fontSize: 13, color: COLORS.muted, marginBottom: 4 }}>Install</div>
                    <div style={{ fontSize: 28, fontWeight: 800, color: COLORS.per, fontFamily: "'Playfair Display', serif" }}>{fmt(assumptions.per_install)}</div>
                  </div>
                  <div style={{ borderLeft: `1px solid ${COLORS.border}`, paddingLeft: 24 }}>
                    <div style={{ fontSize: 13, color: COLORS.muted, marginBottom: 4 }}>ARR</div>
                    <div style={{ fontSize: 28, fontWeight: 800, color: COLORS.per, fontFamily: "'Playfair Display', serif" }}>{fmt(assumptions.per_arr_annual)}</div>
                  </div>
                </div>
                <div style={{ background: COLORS.bg, borderRadius: 8, padding: "10px 16px", marginBottom: 12 }}>
                  <div style={{ fontSize: 13, color: COLORS.muted }}>5-Year Contract Value</div>
                  <div style={{ fontSize: 22, fontWeight: 700, color: COLORS.per, fontFamily: "'Playfair Display', serif" }}>{fmt(assumptions.per_contract)}</div>
                </div>
              </div>
              <div style={{ flex: 1, minWidth: 260, background: COLORS.card, borderRadius: 12, border: `2px solid ${COLORS.sync}`, padding: 32, textAlign: "center" }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: COLORS.sync, textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 16 }}>Sync <span style={{ fontSize: 10, opacity: 0.7 }}>(Optimizer only)</span></div>
                <div style={{ display: "flex", justifyContent: "center", gap: 24, marginBottom: 16 }}>
                  <div>
                    <div style={{ fontSize: 13, color: COLORS.muted, marginBottom: 4 }}>Install</div>
                    <div style={{ fontSize: 28, fontWeight: 800, color: COLORS.sync, fontFamily: "'Playfair Display', serif" }}>—</div>
                  </div>
                  <div style={{ borderLeft: `1px solid ${COLORS.border}`, paddingLeft: 24 }}>
                    <div style={{ fontSize: 13, color: COLORS.muted, marginBottom: 4 }}>ARR</div>
                    <div style={{ fontSize: 28, fontWeight: 800, color: COLORS.sync, fontFamily: "'Playfair Display', serif" }}>{fmt(assumptions.sync_arr_annual)}</div>
                  </div>
                </div>
                <div style={{ background: COLORS.bg, borderRadius: 8, padding: "10px 16px", marginBottom: 12 }}>
                  <div style={{ fontSize: 13, color: COLORS.muted }}>5-Year Contract Value</div>
                  <div style={{ fontSize: 22, fontWeight: 700, color: COLORS.sync, fontFamily: "'Playfair Display', serif" }}>{fmt(assumptions.sync_contract)}</div>
                </div>
              </div>
            </div>

            <SectionTitle title="SAM Revenue Potential" subtitle="One-time install revenue vs recurring revenue for good fits in our regions" />
            <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginBottom: 40 }}>
              {/* Optimizer */}
              <div style={{ flex: 1, minWidth: 300, background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 28 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: COLORS.opt, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 16 }}>Optimizer · 635 buildings in regions</div>
                <div style={{ marginBottom: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 6 }}>
                    <span style={{ color: COLORS.muted }}>One-Time Install</span>
                    <span style={{ fontWeight: 700, color: COLORS.text }}>{fmt(summary.region_opt_fits * assumptions.opt_install)}</span>
                  </div>
                  <div style={{ background: COLORS.bg, borderRadius: 6, height: 20, overflow: "hidden" }}>
                    <div style={{ background: COLORS.opt, height: "100%", width: `${(assumptions.opt_install) / (assumptions.opt_contract) * 100}%`, borderRadius: 6 }} />
                  </div>
                </div>
                <div style={{ marginBottom: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 6 }}>
                    <span style={{ color: COLORS.muted }}>ARR · {fmt(assumptions.opt_arr_annual)}/yr × 5yr</span>
                    <span style={{ fontWeight: 700, color: COLORS.text }}>{fmt(summary.region_opt_fits * assumptions.opt_arr_annual * 5)}</span>
                  </div>
                  <div style={{ background: COLORS.bg, borderRadius: 6, height: 20, overflow: "hidden" }}>
                    <div style={{ background: COLORS.per, height: "100%", width: `${(assumptions.opt_arr_annual * 5) / (assumptions.opt_contract) * 100}%`, borderRadius: 6 }} />
                  </div>
                </div>
                <div style={{ borderTop: `1px solid ${COLORS.border}`, paddingTop: 12, display: "flex", justifyContent: "space-between" }}>
                  <span style={{ fontSize: 13, fontWeight: 600, color: COLORS.muted }}>Total Contract Value</span>
                  <span style={{ fontSize: 18, fontWeight: 800, color: COLORS.opt, fontFamily: "'Playfair Display', serif" }}>{fmt(summary.region_opt_fits * assumptions.opt_contract)}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: COLORS.muted, marginTop: 4 }}>
                  <span>Annual ARR only</span>
                  <span style={{ fontWeight: 600 }}>{fmt(summary.region_opt_fits * assumptions.opt_arr_annual)}/yr</span>
                </div>
              </div>
              {/* Periscope */}
              <div style={{ flex: 1, minWidth: 300, background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 28 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: COLORS.per, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 16 }}>Periscope · 814 buildings in regions</div>
                <div style={{ marginBottom: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 6 }}>
                    <span style={{ color: COLORS.muted }}>One-Time Install</span>
                    <span style={{ fontWeight: 700, color: COLORS.text }}>{fmt(summary.region_per_fits * assumptions.per_install)}</span>
                  </div>
                  <div style={{ background: COLORS.bg, borderRadius: 6, height: 20, overflow: "hidden" }}>
                    <div style={{ background: COLORS.opt, height: "100%", width: `${(assumptions.per_install) / (assumptions.per_contract) * 100}%`, borderRadius: 6 }} />
                  </div>
                </div>
                <div style={{ marginBottom: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 6 }}>
                    <span style={{ color: COLORS.muted }}>ARR · {fmt(assumptions.per_arr_annual)}/yr × 5yr</span>
                    <span style={{ fontWeight: 700, color: COLORS.text }}>{fmt(summary.region_per_fits * assumptions.per_arr_annual * 5)}</span>
                  </div>
                  <div style={{ background: COLORS.bg, borderRadius: 6, height: 20, overflow: "hidden" }}>
                    <div style={{ background: COLORS.per, height: "100%", width: `${(assumptions.per_arr_annual * 5) / (assumptions.per_contract) * 100}%`, borderRadius: 6 }} />
                  </div>
                </div>
                <div style={{ borderTop: `1px solid ${COLORS.border}`, paddingTop: 12, display: "flex", justifyContent: "space-between" }}>
                  <span style={{ fontSize: 13, fontWeight: 600, color: COLORS.muted }}>Total Contract Value</span>
                  <span style={{ fontSize: 18, fontWeight: 800, color: COLORS.per, fontFamily: "'Playfair Display', serif" }}>{fmt(summary.region_per_fits * assumptions.per_contract)}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: COLORS.muted, marginTop: 4 }}>
                  <span>Annual ARR only</span>
                  <span style={{ fontWeight: 600 }}>{fmt(summary.region_per_fits * assumptions.per_arr_annual)}/yr</span>
                </div>
              </div>
              {/* Sync */}
              <div style={{ flex: 1, minWidth: 300, background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 28 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: COLORS.sync, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 16 }}>Sync · 635 buildings in regions <span style={{ fontSize: 10, opacity: 0.7, textTransform: "none", letterSpacing: 0 }}>(Optimizer only)</span></div>
                <div style={{ marginBottom: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 6 }}>
                    <span style={{ color: COLORS.muted }}>One-Time Install</span>
                    <span style={{ fontWeight: 700, color: COLORS.text }}>—</span>
                  </div>
                  <div style={{ background: COLORS.bg, borderRadius: 6, height: 20, overflow: "hidden" }} />
                </div>
                <div style={{ marginBottom: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 6 }}>
                    <span style={{ color: COLORS.muted }}>ARR · {fmt(assumptions.sync_arr_annual)}/yr × 5yr</span>
                    <span style={{ fontWeight: 700, color: COLORS.text }}>{fmt(summary.region_opt_fits * assumptions.sync_contract)}</span>
                  </div>
                  <div style={{ background: COLORS.bg, borderRadius: 6, height: 20, overflow: "hidden" }}>
                    <div style={{ background: COLORS.sync, height: "100%", width: "100%", borderRadius: 6 }} />
                  </div>
                </div>
                <div style={{ borderTop: `1px solid ${COLORS.border}`, paddingTop: 12, display: "flex", justifyContent: "space-between" }}>
                  <span style={{ fontSize: 13, fontWeight: 600, color: COLORS.muted }}>Total Contract Value</span>
                  <span style={{ fontSize: 18, fontWeight: 800, color: COLORS.sync, fontFamily: "'Playfair Display', serif" }}>{fmt(summary.region_opt_fits * assumptions.sync_contract)}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: COLORS.muted, marginTop: 4 }}>
                  <span>Annual ARR only</span>
                  <span style={{ fontWeight: 600 }}>{fmt(summary.region_opt_fits * assumptions.sync_arr_annual)}/yr</span>
                </div>
              </div>
            </div>

            {/* Combined totals bar */}
            <div style={{ background: COLORS.opt, borderRadius: 12, padding: 28, marginBottom: 40, display: "flex", justifyContent: "space-around", flexWrap: "wrap", gap: 16 }}>
              <div style={{ textAlign: "center" }}>
                <div style={{ fontSize: 12, color: "rgba(255,255,255,0.6)", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6 }}>Total Install Revenue</div>
                <div style={{ fontSize: 28, fontWeight: 800, color: "#fff", fontFamily: "'Playfair Display', serif" }}>{fmt(summary.region_opt_fits * assumptions.opt_install + summary.region_per_fits * assumptions.per_install)}</div>
              </div>
              <div style={{ textAlign: "center" }}>
                <div style={{ fontSize: 12, color: "rgba(255,255,255,0.6)", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6 }}>Total Annual ARR</div>
                <div style={{ fontSize: 28, fontWeight: 800, color: COLORS.accent, fontFamily: "'Playfair Display', serif" }}>{fmt(summary.region_opt_fits * assumptions.opt_arr_annual + summary.region_per_fits * assumptions.per_arr_annual + summary.region_opt_fits * assumptions.sync_arr_annual)}</div>
              </div>
              <div style={{ textAlign: "center" }}>
                <div style={{ fontSize: 12, color: "rgba(255,255,255,0.6)", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6 }}>5-Year SAM Total</div>
                <div style={{ fontSize: 28, fontWeight: 800, color: "#fff", fontFamily: "'Playfair Display', serif" }}>{fmt(summary.sam_revenue)}</div>
              </div>
            </div>

            <SectionTitle title="SAM Analysis" subtitle="Good fits in our regions — how much is already captured" />
            <div style={{ background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 32 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 24, marginBottom: 24 }}>
                <div style={{ flex: 1 }}>
                  <div style={{ background: COLORS.bg, borderRadius: 8, height: 40, position: "relative", overflow: "hidden" }}>
                    <div style={{ background: COLORS.accent, height: "100%", width: `${(summary.current_revenue / summary.sam_revenue) * 100}%`, borderRadius: 8, display: "flex", alignItems: "center", justifyContent: "center", minWidth: 80 }}>
                      <span style={{ color: "#fff", fontWeight: 700, fontSize: 13 }}>{(summary.current_revenue / summary.sam_revenue * 100).toFixed(1)}%</span>
                    </div>
                  </div>
                </div>
              </div>
              <div style={{ display: "flex", gap: 32 }}>
                <div><span style={{ fontSize: 13, color: COLORS.muted }}>Remaining SAM: </span><span style={{ fontWeight: 700, fontSize: 15, color: COLORS.per }}>{fmt(summary.sam_revenue - summary.current_revenue)}</span></div>
                <div><span style={{ fontSize: 13, color: COLORS.muted }}>SAM: </span><span style={{ fontWeight: 700, fontSize: 15 }}>{fmt(summary.sam_revenue)}</span></div>
              </div>
            </div>

            <SectionTitle title="SOM Analysis" subtitle={`${somPercent}% of SAM in our regions — how much is already captured`} />
            <div style={{ background: COLORS.card, borderRadius: 12, border: `1px solid ${COLORS.border}`, padding: 32 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 24, marginBottom: 24 }}>
                <div style={{ flex: 1 }}>
                  <div style={{ background: COLORS.bg, borderRadius: 8, height: 40, position: "relative", overflow: "hidden" }}>
                    <div style={{ background: COLORS.accent, height: "100%", width: `${(summary.current_revenue / somRevenue) * 100}%`, borderRadius: 8, display: "flex", alignItems: "center", justifyContent: "center", minWidth: 80 }}>
                      <span style={{ color: "#fff", fontWeight: 700, fontSize: 13 }}>{(summary.current_revenue / somRevenue * 100).toFixed(1)}%</span>
                    </div>
                  </div>
                </div>
              </div>
              <div style={{ display: "flex", gap: 32 }}>
                <div><span style={{ fontSize: 13, color: COLORS.muted }}>Remaining SOM: </span><span style={{ fontWeight: 700, fontSize: 15, color: COLORS.per }}>{fmt(somRevenue - summary.current_revenue)}</span></div>
                <div><span style={{ fontSize: 13, color: COLORS.muted }}>SOM: </span><span style={{ fontWeight: 700, fontSize: 15 }}>{fmt(somRevenue)}</span></div>
              </div>
            </div>
          </>
        )}

        {/* ROADMAP TAB */}
        {/* Footer */}
        <div style={{ marginTop: 64, paddingTop: 24, borderTop: `1px solid ${COLORS.border}` }} />
      </div>
    </div>
  );
}
