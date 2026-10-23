// Copyright (c) 2026, Reformiqo and contributors
// For license information, please see license.txt

// The control plane's one screen: what every site is spending, and which model
// they are all answering with. Everything here reads from one round trip so the
// page does not flicker in six places while it loads.

frappe.pages["ai-control"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("AI Control"),
		single_column: true,
	});
	new AIControl(page);
};

const esc = frappe.utils.escape_html;
// Every cost on this page is already rupees: cost_of() converts a provider's
// USD rate at the configured usd_inr before it writes the row. The dollar sign
// here was a label on a number that had never been a dollar.
const money = (v, dp = 4) =>
	"\u20B9" + Number(v || 0).toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp });
const num = (v) => Number(v || 0).toLocaleString("en-IN");

class AIControl {
	constructor(page) {
		this.page = page;
		this.render_shell();
		this.add_filters();
		this.refresh();
	}

	add_filters() {
		this.from_date = this.page.add_field({
			fieldname: "from_date",
			label: __("From"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -30),
			change: () => this.refresh(),
		});
		this.to_date = this.page.add_field({
			fieldname: "to_date",
			label: __("To"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			change: () => this.refresh(),
		});
		this.page.set_primary_action(__("Refresh"), () => this.refresh(), "refresh");
		this.page.add_menu_item(__("All call records"), () =>
			frappe.set_route("List", "AI Call Record")
		);
		this.page.add_menu_item(__("Spend report"), () =>
			frappe.set_route("query-report", "AI Spend")
		);
		this.page.add_menu_item(__("Sites"), () => frappe.set_route("List", "Managed Site"));
		this.page.add_menu_item(__("Provider credentials"), () =>
			frappe.set_route("List", "AI Provider Credential")
		);
	}

	render_shell() {
		this.$body = $(`
			<div class="aic">
				<style>
					.aic{padding:2px 0 40px}
					.aic .sec{margin-top:22px}
					.aic .sec-h{display:flex;align-items:baseline;gap:10px;margin-bottom:10px}
					.aic .sec-h h3{font-size:14px;font-weight:600;margin:0;color:var(--heading-color)}
					.aic .sec-h span{font-size:12px;color:var(--text-muted)}
					.aic .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
					.aic .card{background:var(--card-bg);border:1px solid var(--border-color);
						border-radius:var(--border-radius-md,8px);padding:12px 14px}
					.aic .card .k{font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em}
					.aic .card .v{font-size:21px;font-weight:600;margin-top:3px;color:var(--heading-color);
						font-variant-numeric:tabular-nums}
					.aic .card .s{font-size:11px;color:var(--text-muted);margin-top:2px}
					.aic .warn{color:var(--text-on-orange,#9c4d16)}
					.aic .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
					@media (max-width:900px){.aic .grid2{grid-template-columns:1fr}}
					.aic table{width:100%;border-collapse:collapse;font-size:12.5px}
					.aic th{text-align:left;font-weight:500;color:var(--text-muted);padding:7px 10px;
						border-bottom:1px solid var(--border-color);white-space:nowrap;font-size:11px;
						text-transform:uppercase;letter-spacing:.04em}
					.aic td{padding:7px 10px;border-bottom:1px solid var(--border-color);white-space:nowrap}
					.aic tr:last-child td{border-bottom:0}
					.aic .r{text-align:right;font-variant-numeric:tabular-nums}
					.aic .box{background:var(--card-bg);border:1px solid var(--border-color);
						border-radius:var(--border-radius-md,8px);overflow:hidden}
					.aic .box .scroll{overflow-x:auto}
					.aic .model-bar{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end}
					.aic .model-bar .fld{display:flex;flex-direction:column;gap:4px;min-width:170px}
					.aic .model-bar label{font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em}
					.aic .now{font-size:12px;color:var(--text-muted);margin-top:10px}
					.aic .now b{color:var(--heading-color);font-weight:600}
					.aic .pill{display:inline-block;font-size:11px;padding:1px 8px;border-radius:999px;
						background:var(--control-bg);color:var(--text-muted)}
					.aic .pill.bad{background:var(--bg-red,#fde8e8);color:var(--text-on-red,#9b1c1c)}
					.aic .empty{padding:26px;text-align:center;color:var(--text-muted);font-size:13px}
					.aic .spark{display:flex;align-items:flex-end;gap:3px;height:56px;padding:8px 12px 10px;
						border-top:1px solid var(--border-color)}
					/* Fixed-width bars: flex:1 made two days fill the whole card. */
					.aic .spark i{flex:0 0 9px;width:9px;background:var(--blue-500,#2490ef);
						opacity:.75;border-radius:1px;min-height:2px}
				</style>
				<div class="model-sec sec"></div>
				<div class="kpi-sec sec"></div>
				<div class="split-sec sec grid2"></div>
				<div class="recent-sec sec"></div>
			</div>
		`).appendTo(this.page.main);
	}

	refresh() {
		const args = {
			from_date: this.from_date ? this.from_date.get_value() : null,
			to_date: this.to_date ? this.to_date.get_value() : null,
		};
		frappe.call({ method: "erpera_admin.dashboard.summary", args, freeze: false })
			.then((r) => {
				if (!r || !r.message) return;
				this.data = r.message;
				this.draw();
			})
			.catch(() => {
				this.$body.find(".kpi-sec").html(
					`<div class="box"><div class="empty">${__("Could not load. Check the server log.")}</div></div>`
				);
			});
	}

	draw() {
		this.draw_model();
		this.draw_kpis();
		this.draw_split();
		this.draw_recent();
	}

	draw_model() {
		const { config, providers, settings } = this.data;
		const opts = (sel) =>
			['<option value="">' + __("Not set") + "</option>"]
				.concat(
					providers.map(
						(p) =>
							`<option value="${esc(p.name)}" ${sel === p.name ? "selected" : ""}>` +
							`${esc(p.name)} (${esc(p.provider)})${p.enabled ? "" : " (disabled)"}</option>`
					)
				)
				.join("");

		// resolved_config() hands back what a site would actually receive, so the
		// line below is the real answer, not a restatement of the form fields.
		const prim = config.primary;
		const fb = config.fallback;
		const now = prim
			? `Every site answers with <b>${esc(prim.provider)}</b> / <b>${esc(prim.model || "(provider default)")}</b>` +
			  (fb ? `, falling back to <b>${esc(fb.provider)}</b> / <b>${esc(fb.model || "(provider default)")}</b>.` : ", with no fallback configured.")
			: `<b>${__("No provider is set")}.</b> Sites fall back to whatever is configured locally.`;

		this.$body.find(".model-sec").html(`
			<div class="sec-h"><h3>${__("Model every site uses")}</h3>
				<span>${__("Changed here, picked up by each site on its next config pull.")}</span></div>
			<div class="box" style="padding:16px">
				<div class="model-bar">
					<div class="fld"><label>${__("Provider")}</label>
						<select class="form-control input-sm sel-prov">${opts(settings.active_provider)}</select></div>
					<div class="fld"><label>${__("Model")}</label>
						<input type="text" class="form-control input-sm in-model" placeholder="${__("provider default")}" value="${esc(settings.active_model)}"></div>
					<div class="fld"><label>${__("Fallback provider")}</label>
						<select class="form-control input-sm sel-fb">${opts(settings.fallback_provider)}</select></div>
					<div class="fld"><label>${__("Fallback model")}</label>
						<input type="text" class="form-control input-sm in-fbmodel" placeholder="${__("provider default")}" value="${esc(settings.fallback_model)}"></div>
					<button class="btn btn-primary btn-sm btn-save">${__("Apply")}</button>
				</div>
				<div class="now">${now}</div>
			</div>
		`);

		this.$body.find(".btn-save").on("click", () => {
			const $b = this.$body;
			frappe.call({
				method: "erpera_admin.dashboard.set_model",
				args: {
					provider: $b.find(".sel-prov").val(),
					model: $b.find(".in-model").val(),
					fallback_provider: $b.find(".sel-fb").val(),
					fallback_model: $b.find(".in-fbmodel").val(),
				},
				freeze: true,
				freeze_message: __("Applying"),
			}).then(() => {
				frappe.show_alert({ message: __("Model updated for every site"), indicator: "green" });
				this.refresh();
			});
		});
	}

	draw_kpis() {
		const t = this.data.totals || {};
		const costPerTurn = t.turns ? t.cost / t.turns : 0;
		const card = (k, v, s, warn) =>
			`<div class="card"><div class="k">${k}</div>
			 <div class="v ${warn ? "warn" : ""}">${v}</div>
			 ${s ? `<div class="s">${s}</div>` : ""}</div>`;

		this.$body.find(".kpi-sec").html(`
			<div class="sec-h"><h3>${__("Usage")}</h3>
				<span>${esc(this.data.range.from_date)} → ${esc(this.data.range.to_date)}</span></div>
			<div class="cards">
				${card(__("Questions"), num(t.turns), __("{0} model calls", [num(t.calls)]))}
				${card(__("Spend"), money(t.cost), __("{0} per question", [money(costPerTurn, 6)]))}
				${card(__("Tokens"), num((t.input_tokens || 0) + (t.output_tokens || 0)),
					__("{0} in / {1} out", [num(t.input_tokens), num(t.output_tokens)]))}
				${card(__("Avg call"), num(Math.round(t.avg_ms)) + " ms", __("model time only"))}
				${card(__("Sites"), num(t.sites), __("reporting in this window"))}
				${card(__("Failed"), num(t.failed), __("{0} unpriced", [num(t.unrated)]),
					(t.failed || 0) > 0 || (t.unrated || 0) > 0)}
			</div>
		`);
	}

	draw_split() {
		const bySite = this.data.by_site || [];
		const byModel = this.data.by_model || [];
		const max = Math.max(1, ...(this.data.by_day || []).map((d) => d.calls));
		const spark = (this.data.by_day || [])
			.map((d) => `<i style="height:${Math.max(2, Math.round((d.calls / max) * 100))}%" title="${esc(d.d)}: ${d.calls}"></i>`)
			.join("");

		const siteRows = bySite.length
			? bySite
					.map(
						(s) => `<tr>
						<td><a href="/app/managed-site/${encodeURIComponent(s.site || "")}">${esc(s.site || "-")}</a></td>
						<td class="r">${num(s.turns)}</td>
						<td class="r">${num(s.calls)}</td>
						<td class="r">${num(s.tokens)}</td>
						<td class="r">${money(s.cost)}</td>
						<td class="r">${s.failed ? `<span class="pill bad">${num(s.failed)}</span>` : "-"}</td>
					</tr>`
					)
					.join("")
			: `<tr><td colspan="6"><div class="empty">${__("No site has reported a call in this window.")}</div></td></tr>`;

		const modelRows = byModel.length
			? byModel
					.map(
						(m) => `<tr>
						<td>${esc(m.provider || "-")}</td>
						<td>${esc(m.model || "-")}</td>
						<td class="r">${num(m.calls)}</td>
						<td class="r">${num(m.tokens)}</td>
						<td class="r">${money(m.cost)}${m.unrated ? ' <span class="warn" title="' + __("unpriced") + '">•</span>' : ""}</td>
					</tr>`
					)
					.join("")
			: `<tr><td colspan="5"><div class="empty">${__("Nothing yet.")}</div></td></tr>`;

		this.$body.find(".split-sec").html(`
			<div>
				<div class="sec-h"><h3>${__("By site")}</h3></div>
				<div class="box"><div class="scroll"><table>
					<thead><tr><th>${__("Site")}</th><th class="r">${__("Questions")}</th>
						<th class="r">${__("Calls")}</th><th class="r">${__("Tokens")}</th>
						<th class="r">${__("Cost")}</th><th class="r">${__("Failed")}</th></tr></thead>
					<tbody>${siteRows}</tbody>
				</table></div></div>
			</div>
			<div>
				<div class="sec-h"><h3>${__("By model")}</h3>
					<span>${__("Daily call volume below")}</span></div>
				<div class="box"><div class="scroll"><table>
					<thead><tr><th>${__("Provider")}</th><th>${__("Model")}</th>
						<th class="r">${__("Calls")}</th><th class="r">${__("Tokens")}</th>
						<th class="r">${__("Cost")}</th></tr></thead>
					<tbody>${modelRows}</tbody>
				</table></div>
				${spark ? `<div class="spark">${spark}</div>` : ""}
				</div>
			</div>
		`);
	}

	draw_recent() {
		const rows = this.data.recent || [];
		const body = rows.length
			? rows
					.map(
						(c) => `<tr>
					<td>${frappe.datetime.str_to_user(c.occurred_on)}</td>
					<td>${esc(c.site || "-")}</td>
					<td>${esc(c.user || "-")}</td>
					<td>${esc(c.channel || "-")}</td>
					<td>${esc(c.agent || "-")}</td>
					<td>${esc(c.model || "-")}</td>
					<td class="r">${num(c.input_tokens)}/${num(c.output_tokens)}</td>
					<td class="r">${num(c.duration_ms)} ms</td>
					<td class="r">${money(c.cost, 6)}${c.rated ? "" : ' <span class="warn">•</span>'}</td>
					<td>${c.status === "Failed" ? `<span class="pill bad">${__("Failed")}</span>` : ""}</td>
					<td><a href="/app/ai-call-record/${encodeURIComponent(c.name)}">${__("Open")}</a></td>
				</tr>`
					)
					.join("")
			: `<tr><td colspan="11"><div class="empty">${__(
					"No calls recorded yet. Point a site at this one and it will fill up."
			  )}</div></td></tr>`;

		this.$body.find(".recent-sec").html(`
			<div class="sec-h"><h3>${__("Recent calls")}</h3>
				<span>${__("newest 40")} &middot; <span class="warn">•</span> ${__("means no price is set for that model")}</span></div>
			<div class="box"><div class="scroll"><table>
				<thead><tr><th>${__("When")}</th><th>${__("Site")}</th><th>${__("User")}</th>
					<th>${__("Channel")}</th><th>${__("Agent")}</th><th>${__("Model")}</th>
					<th class="r">${__("In/Out")}</th><th class="r">${__("Took")}</th>
					<th class="r">${__("Cost")}</th><th></th><th></th></tr></thead>
				<tbody>${body}</tbody>
			</table></div></div>
		`);
	}
}
