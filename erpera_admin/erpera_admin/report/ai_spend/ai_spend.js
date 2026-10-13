// Copyright (c) 2026, Reformiqo and contributors
// For license information, please see license.txt

frappe.query_reports["AI Spend"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -30),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "group_by",
			label: __("Group By"),
			fieldtype: "Select",
			options: ["Site", "Model", "Provider", "User", "Channel", "Agent", "Day"],
			default: "Site",
			reqd: 1,
		},
		{ fieldname: "site", label: __("Site"), fieldtype: "Link", options: "Managed Site" },
		{ fieldname: "provider", label: __("Provider"), fieldtype: "Data" },
		{ fieldname: "model", label: __("Model"), fieldtype: "Data" },
		{
			fieldname: "channel",
			label: __("Channel"),
			fieldtype: "Select",
			options: ["", "Desk", "WhatsApp", "API", "Background"],
		},
	],

	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		// An unpriced call reads as a free one, which is the most misleading
		// thing a spend report can do. Flag it rather than hide it.
		if (["unrated", "failed"].includes(column.fieldname) && data && data[column.fieldname] > 0) {
			value = `<span style="color:var(--text-on-orange,#9c4d16)">${value}</span>`;
		}
		return value;
	},
};
