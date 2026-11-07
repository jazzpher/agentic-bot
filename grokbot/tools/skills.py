"""Skill tools (procedural memory) backed by the existing SkillStore."""
import json

from grokbot.tools import ToolError


def register(registry):
    @registry.tool('list_skills', 'List saved skills (reusable workflows). Optional keyword search and offset for paging.',
                   {'type': 'object', 'properties': {
                       'query': {'type': 'string', 'description': 'Keyword search over name/description'},
                       'offset': {'type': 'integer'}},
                    'required': []}, category='skills')
    def list_skills(ctx, query='', offset=0):
        try:
            return json.dumps(ctx.workspace.skills.list_page(query or '', max(0, int(offset or 0))), ensure_ascii=False)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @registry.tool('read_skill', 'Load a saved skill by name and mark it used. Returns its Markdown and revision.',
                   {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']}, category='skills')
    def read_skill(ctx, name):
        try:
            return json.dumps(ctx.workspace.skills.use(name), ensure_ascii=False)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @registry.tool('save_skill', 'Stage a reusable workflow; saved only after independent acceptance and successful completion. Include when to use it, '
                   'numbered steps, pitfalls and verification. Use generic placeholders, never secrets or private data. '
                   'For updates pass the revision returned by read_skill.',
                   {'type': 'object', 'properties': {
                       'name': {'type': 'string', 'description': 'lowercase-with-hyphens'},
                       'description': {'type': 'string', 'description': 'One line: when to use it'},
                       'content': {'type': 'string', 'description': 'Markdown body'},
                       'revision': {'type': 'string', 'description': 'Existing revision when updating; omit for new'}},
                    'required': ['name', 'description', 'content']}, category='skills', mutates=True)
    def save_skill(ctx, name, description, content, revision=''):
        if not ctx.extras.get('_skill_learning_enabled'):
            raise ToolError('Skill learning requires host requirements and an independent completion verifier.')
        pending = ctx.extras['_skill_candidates']
        if len(pending) >= 3:
            raise ToolError('At most three skill candidates per turn.')
        try:
            ctx.workspace.skills.validate(name, description, content)
            pending.append((name, description, content, revision or ''))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return 'Staged skill ' + name + '. Not saved yet: independent acceptance must pass and this turn must complete.'
