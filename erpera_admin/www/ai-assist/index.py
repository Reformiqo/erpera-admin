import frappe

# The marketing page is public by design - no login, and safe to cache.
no_cache = 0


def get_context(context):
	context.no_header = 1
	context.no_breadcrumbs = 1
	return context
