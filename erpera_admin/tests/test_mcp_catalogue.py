"""What the connector catalogue must keep being true.

The tool list an MCP client is shown is served from here rather than shipped
with the customer app, because the customer is Administrator on the site the
connector runs on. That makes this file the only place some of the product's
promises can be checked: a connector that cannot submit, a read-only mode that
is told nothing about the write tools, and a prompt whose template cannot refer
to an argument the client was never asked for.
"""

import re
import unittest

from erpera_admin.brain import tools as brain_tools

# Anything that would change a document's docstatus, or remove it. The promise
# made to a customer is not "the model is told not to", it is that the verb is
# absent on both sides of the wire.
FORBIDDEN = {"submit", "submit_document", "cancel", "cancel_record", "amend",
             "delete", "delete_record", "remove_record", "run_sql", "run_query",
             "run_python", "execute_sql"}


class TestCatalogue(unittest.TestCase):
	def setUp(self):
		self.tools = brain_tools.mcp_catalogue()
		self.by_name = {t["name"]: t for t in self.tools}

	def test_no_tool_can_submit_or_delete(self):
		for banned in FORBIDDEN:
			self.assertNotIn(banned, self.by_name,
			                 f"{banned} is in the connector catalogue")

	def test_names_are_unique(self):
		names = [t["name"] for t in self.tools]
		self.assertEqual(len(names), len(set(names)), f"duplicate tool name in {names}")

	def test_every_tool_is_callable_as_described(self):
		for t in self.tools:
			name = t.get("name")
			self.assertTrue(name, f"tool with no name: {t}")
			self.assertTrue(t.get("description"), f"{name} has no description")
			schema = t.get("inputSchema")
			self.assertIsInstance(schema, dict, f"{name} has no inputSchema")
			self.assertEqual(schema.get("type"), "object",
			                 f"{name} inputSchema is not an object")
			self.assertIsInstance(schema.get("properties"), dict,
			                      f"{name} inputSchema has no properties")
			for req in schema.get("required", []):
				self.assertIn(req, schema["properties"],
				              f"{name} requires {req}, which is not a property")

	def test_annotations_say_which_tools_write(self):
		# mcp_read_only on the customer site filters on readOnlyHint alone, so a
		# wrong hint here hands a write tool to a read-only connector.
		writers = {"create_record", "update_record", "add_comment"}
		for t in self.tools:
			ann = t.get("annotations") or {}
			self.assertIn("readOnlyHint", ann, f"{t['name']} has no readOnlyHint")
			self.assertEqual(ann["readOnlyHint"], t["name"] not in writers,
			                 f"{t['name']} has the wrong readOnlyHint")
			self.assertFalse(ann.get("destructiveHint"),
			                 f"{t['name']} is marked destructive; nothing here destroys anything")
			self.assertTrue(ann.get("title"), f"{t['name']} has no title")

	def test_context_comes_before_the_tools_that_need_it(self):
		names = [t["name"] for t in self.tools]
		self.assertEqual(names[0], "get_context")
		self.assertLess(names.index("resolve_period"), names.index("list_documents"))
		# Writes last, so a client reading the list top to bottom meets the
		# reads first.
		self.assertGreater(names.index("create_record"), names.index("run_report"))

	def test_the_tools_only_the_connector_gets_are_not_given_to_the_agent(self):
		# The agent has its own prompt telling it the company and the date; these
		# exist because a client inside somebody else's product does not. Adding
		# them to the agent's own lists would change answers that already work.
		agent = {t["function"]["name"] for t in
		         brain_tools.READ_TOOLS + brain_tools.WRITE_TOOLS + brain_tools.NAV_TOOLS}
		for t in brain_tools.MCP_ONLY_TOOLS:
			self.assertNotIn(t["function"]["name"], agent)

	def test_executor_covers_every_shared_tool(self):
		# A tool the brain offers and the site cannot run is offered and then
		# refused. The MCP-only ones run in ai_assist and are not in this source.
		source = open(brain_tools.__file__).read()
		shared = {t["function"]["name"] for t in
		          brain_tools.READ_TOOLS + brain_tools.WRITE_TOOLS}
		for name in shared:
			self.assertIn(f'"{name}"', source, f"{name} has no branch in execute_tool")


class TestPrompts(unittest.TestCase):
	def setUp(self):
		self.prompts = brain_tools.mcp_prompts()

	def test_every_prompt_is_complete(self):
		self.assertTrue(self.prompts)
		for p in self.prompts:
			self.assertTrue(p.get("name"))
			self.assertTrue(p.get("title"), f"{p['name']} has no title")
			self.assertTrue(p.get("description"), f"{p['name']} has no description")
			self.assertTrue(p.get("template"), f"{p['name']} has no template")
			self.assertIsInstance(p.get("arguments"), list)
			for a in p["arguments"]:
				self.assertTrue(a.get("name"))
				self.assertTrue(a.get("description"), f"{p['name']}.{a['name']} unexplained")
				self.assertIn("required", a)

	def test_a_template_only_refers_to_arguments_it_asked_for(self):
		for p in self.prompts:
			asked = {a["name"] for a in p["arguments"]}
			used = set(re.findall(r"(?<!\{)\{([a-z_]+)\}", p["template"]))
			self.assertEqual(used - asked, set(),
			                 f"{p['name']} fills {used - asked}, which it never asks for")

	def test_an_optional_argument_has_something_to_fall_back_on(self):
		for p in self.prompts:
			for a in p["arguments"]:
				if not a["required"]:
					self.assertTrue(a.get("default"),
					                f"{p['name']}.{a['name']} is optional with no default")

	def test_no_prompt_asks_for_a_submit(self):
		for p in self.prompts:
			body = p["template"].lower()
			self.assertNotIn("submit it for me", body)
			if "payment" in p["name"]:
				self.assertIn("cannot submit", body)
