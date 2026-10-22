# ai_assist/engine/nlu_samples.py
#
# Sample phrasings per intent. This file IS the training data for the
# no-model matcher — adding a phrasing here is how you teach it, and it costs
# nothing at runtime beyond a set intersection.
#
# Write how people actually type, including the terse ones ("receivables?"),
# the conversational ones ("who hasn't paid us") and the misspelt shapes you
# see in support chats. Synonyms are already handled in nlu.SYNONYM_GROUPS, so
# do not pad these with "client" when you have written "customer".

SAMPLES = {
	# — dashboard —
	"company_overview": [
		"business overview", "how is the business doing", "company summary",
		"give me an overview", "overall position", "how are we doing",
	],
	"today_summary": [
		"what happened today", "todays activity", "today summary",
		"activity today", "whats going on today",
	],
	# — accounts —
	"outstanding_receivables": [
		"who owes us money", "outstanding receivables", "how much are we owed",
		"unpaid customer invoices", "who has not paid us", "money to collect",
		"customer dues", "pending payments from customers", "receivables",
		"who hasnt paid us", "customers who have not paid", "payments owed to us",
		"total outstanding", "total outstanding from customers", "what is our total outstanding",
		"how much do customers owe us", "outstanding amount from customers",
		"total receivables", "total amount outstanding", "outstanding balance from customers",
		"how much money is outstanding", "sum of outstanding invoices",
	],
	"outstanding_payables": [
		"who do we owe", "outstanding payables", "how much do we owe suppliers",
		"unpaid supplier bills", "supplier dues", "payables", "our pending bills",
		"bills we have not paid", "what we owe to vendors",
		"total payables", "total outstanding to suppliers", "how much do we owe in total",
		"outstanding amount to suppliers",
	],
	"recent_payments": [
		"recent payments", "latest payments received", "payments this week",
		"who paid recently", "payment entries",
	],
	"profit_and_loss": [
		"profit and loss", "are we profitable", "p and l statement", "pnl",
		"net profit this year", "income statement", "how much profit",
	],
	"trial_balance": ["trial balance", "tb report", "balances of all accounts"],
	"bank_balance": [
		"bank balance", "how much cash do we have", "cash position",
		"money in the bank", "available funds",
	],
	"overdue_receivables": [
		"overdue invoices", "late paying customers", "invoices past due date",
		"which invoices are overdue", "customers who are late",
	],
	"monthly_revenue": [
		"monthly revenue trend", "revenue by month", "sales trend over months",
		"how has revenue changed",
	],
	"receivable_aging_report": [
		"receivable aging", "how old are our receivables", "aging of customer dues",
		"ageing report for customers",
	],
	"payable_aging_report": ["payable aging", "how old are supplier dues", "ageing of payables"],
	"gl_entries": ["general ledger", "gl entries", "ledger postings", "accounting entries"],
	"journal_entries": ["journal entries", "manual entries", "journal vouchers"],
	"tax_summary": ["tax summary", "gst summary", "how much tax", "vat collected"],
	"expense_claims": ["expense claims", "staff reimbursements", "employee expenses"],
	# — master data —
	"customer_list": [
		"how many customers do we have", "list of customers", "all customers",
		"customer list", "show me customers", "our customers", "customer master",
		"count of customers", "how many clients",
	],
	"supplier_list": [
		"how many suppliers do we have", "list of suppliers", "all suppliers",
		"supplier list", "our vendors", "supplier master", "count of suppliers",
	],
	"item_list": [
		"how many items do we have", "list of items", "all products",
		"item list", "item master", "what products do we sell", "count of items",
	],
	# — sales —
	"open_sales_orders": [
		"open sales orders", "pending orders from customers", "unfulfilled orders",
		"which orders are still open", "sales orders not delivered",
	],
	"quotation_pipeline": [
		"quotation pipeline", "open quotes", "pending quotations", "deals in pipeline",
	],
	"top_customers": [
		"top customers", "best customers this month", "who buys the most",
		"biggest customers", "highest spending customers",
	],
	"pending_deliveries": [
		"pending deliveries", "what needs to be delivered", "undelivered orders",
		"deliveries due",
	],
	"sales_this_month": [
		"sales this month", "how much have we sold this month", "monthly sales",
		"revenue this month", "what are our sales",
	],
	"top_selling_items": [
		"top selling items", "best selling products", "which items sell most",
		"most popular products",
	],
	"sales_by_territory": ["sales by territory", "revenue by region", "which area sells most"],
	"sales_by_item_group": ["sales by item group", "revenue by product category"],
	"delivery_notes": ["delivery notes", "dispatch notes", "goods delivered"],
	"credit_notes": ["credit notes", "sales returns", "customer refunds"],
	"quotation_conversion_rate": [
		"quotation win rate", "how many quotes convert", "quote conversion rate",
		"win ratio on quotations",
	],
	# — purchase —
	"pending_purchase_orders": [
		"pending purchase orders", "open po", "orders we placed on suppliers",
		"purchase orders not received",
	],
	"supplier_payments_due": [
		"supplier payments due", "what do we need to pay", "upcoming supplier payments",
	],
	"top_suppliers": ["top suppliers", "biggest suppliers", "who we buy most from"],
	"purchase_this_month": [
		"purchases this month", "how much did we buy", "monthly purchase value", "spend this month",
	],
	"purchase_receipts": ["purchase receipts", "goods received", "grn"],
	"debit_notes": ["debit notes", "purchase returns", "returns to supplier"],
	"material_requests": ["material requests", "indents", "requests for material"],
	# — inventory —
	"stock_balance": [
		"stock balance", "how much stock do we have", "current inventory",
		"what is in stock", "available quantity", "stock on hand",
		"show stock", "stock report", "stock list", "quantity in hand",
	],
	"low_stock_items": [
		"low stock", "what needs restocking", "items running out", "below reorder level",
		"stock shortage", "which items are short",
	],
	"recent_stock_movement": ["recent stock entries", "stock movements", "latest stock transactions"],
	"stock_valuation": ["stock valuation", "value of inventory", "how much is our stock worth"],
	"warehouse_summary": ["warehouse summary", "stock by warehouse", "inventory per location"],
	"item_price_list": ["item prices", "price list", "what do we charge for items", "selling rates"],
	"stock_ledger": ["stock ledger", "item movement history", "stock transactions for an item"],
	# — hr —
	"employee_directory": [
		"employee directory", "list of staff", "who works here", "all employees", "staff list",
	],
	"attendance_today": [
		"todays attendance", "who is present today", "attendance for today", "who came in",
	],
	"pending_leaves": [
		"pending leave requests", "leave applications to approve", "who applied for leave",
	],
	"birthdays_this_month": ["birthdays this month", "whose birthday is coming", "staff birthdays"],
	"department_headcount": [
		"department headcount", "how many people per department", "staff by department",
	],
	"leave_balance": ["leave balance", "how much leave is left", "remaining leave days"],
	"salary_register": ["salary register", "payroll for the month", "salary details"],
	# — manufacturing —
	"open_work_orders": ["open work orders", "production in progress", "active work orders"],
	"production_progress": ["production progress", "how is production going", "manufacturing status"],
	"overdue_work_orders": ["overdue work orders", "late production", "delayed manufacturing"],
	"quality_summary": ["quality summary", "inspection results", "qc report"],
	"bom_cost_analysis": ["bom cost", "cost of making an item", "bill of material cost"],
	# — projects —
	"active_projects": ["active projects", "ongoing projects", "projects in progress"],
	"project_tasks": ["open tasks", "pending tasks", "what tasks are outstanding", "todo list"],
}
