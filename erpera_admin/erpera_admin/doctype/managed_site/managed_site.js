// Copyright (c) 2026, Reformiqo and contributors
// For license information, please see license.txt

frappe.ui.form.on("Managed Site", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Show API Key"), () => {
			frappe.call({
				method: "frappe.client.get_password",
				args: { doctype: "Managed Site", name: frm.doc.name, fieldname: "token" },
			}).then((r) => show_key(frm, r.message));
		});

		frm.add_custom_button(__("Regenerate API Key"), () => {
			frappe.confirm(
				__("The current key stops working immediately and this site goes quiet until the new key is pasted in. Continue?"),
				() => {
					frm.call("rotate_token").then((r) => {
						frm.reload_doc();
						show_key(frm, r.message, true);
					});
				}
			);
		});

		if (frm.doc.enabled) {
			frm.add_custom_button(__("Suspend"), () => {
				frm.set_value("enabled", 0);
				frm.save().then(() => frappe.show_alert({
					message: __("Suspended. The assistant stops answering within the hour."),
					indicator: "orange",
				}));
			});
		} else {
			frm.dashboard.set_headline(
				__("Suspended. This site's assistant refuses every question.")
			);
		}

		if (frm.doc.valid_until && frm.doc.valid_until < frappe.datetime.get_today()) {
			frm.dashboard.set_headline(__("Expired on {0}.", [frappe.datetime.str_to_user(frm.doc.valid_until)]));
		}
	},
});

function show_key(frm, key, fresh) {
	if (!key) {
		frappe.msgprint(__("No key is set on this site yet. Use Regenerate API Key."));
		return;
	}
	const d = new frappe.ui.Dialog({
		title: fresh ? __("New API Key") : __("API Key"),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "body",
				options: `
					<p style="margin-bottom:8px">${__("Paste this into AI Assist Settings on")} <b>${frappe.utils.escape_html(frm.doc.name)}</b>.</p>
					<div style="font-family:var(--font-stack-mono,monospace);font-size:13px;word-break:break-all;
						background:var(--control-bg);border:1px solid var(--border-color);
						border-radius:6px;padding:10px 12px">${frappe.utils.escape_html(key)}</div>
					<p style="margin-top:10px;color:var(--text-muted);font-size:12px">
						${__("Until it is in place, that site's assistant answers nothing: not on the Desk, not on WhatsApp.")}</p>`,
			},
		],
		primary_action_label: __("Copy"),
		primary_action() {
			frappe.utils.copy_to_clipboard(key);
			d.hide();
		},
	});
	d.show();
}
