# Copyright (c) 2013-2014 Will Thames <will@thames.id.au>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from collections import defaultdict
import os
import re
import sys

import ansiblelint.utils
import codecs

default_rulesdir = os.path.join(os.path.dirname(ansiblelint.utils.__file__), 'rules')


class AnsibleLintRule(object):

    def __repr__(self):
        return self.id + ": " + self.shortdesc

    def verbose(self):
        return self.id + ": " + self.shortdesc + "\n  " + self.description

    match = None
    matchtask = None
    matchplay = None

    @staticmethod
    def unjinja(text):
        return re.sub(r"{{[^}]*}}", "JINJA_VAR", text)

    def matchlines(self, file, text):
        matches = []
        if not self.match:
            return matches
        # arrays are 0-based, line numbers are 1-based
        # so use prev_line_no as the counter
        for (prev_line_no, line) in enumerate(text.split("\n")):
            if line.lstrip().startswith('#'):
                continue

            rule_id_list = ansiblelint.utils.get_rule_skips_from_line(line)
            if self.id in rule_id_list:
                continue

            result = self.match(file, line)
            if not result:
                continue
            message = None
            if isinstance(result, str):
                message = result
            matches.append(Match(prev_line_no + 1, line,
                           file['path'], self, message))
        return matches

    def matchtasks(self, file, text):
        matches = []
        if not self.matchtask:
            return matches

        if file['type'] == 'meta':
            return matches

        yaml = ansiblelint.utils.parse_yaml_linenumbers(text, file['path'])
        if not yaml:
            return matches

        yaml = ansiblelint.utils.append_skipped_rules(yaml, text, file['type'])

        for task in ansiblelint.utils.get_normalized_tasks(yaml, file):
            if self.id in task.get('skipped_rules', ()):
                continue

            if 'action' not in task:
                continue
            result = self.matchtask(file, task)
            if not result:
                continue

            message = None
            if isinstance(result, str):
                message = result
            task_msg = "Task/Handler: " + ansiblelint.utils.task_to_str(task)
            matches.append(Match(task[ansiblelint.utils.LINE_NUMBER_KEY], task_msg,
                           file['path'], self, message))
        return matches

    def matchyaml(self, file, text):
        matches = []
        if not self.matchplay:
            return matches

        yaml = ansiblelint.utils.parse_yaml_linenumbers(text, file['path'])
        if not yaml:
            return matches

        if isinstance(yaml, dict):
            yaml = [yaml]

        yaml = ansiblelint.utils.append_skipped_rules(yaml, text, file['type'])

        for play in yaml:
            if self.id in play.get('skipped_rules', ()):
                continue

            result = self.matchplay(file, play)
            if not result:
                continue

            if isinstance(result, tuple):
                result = [result]

            if not isinstance(result, list):
                raise TypeError("{} is not a list".format(result))

            for section, message in result:
                matches.append(Match(play[ansiblelint.utils.LINE_NUMBER_KEY],
                                     section, file['path'], self, message))
        return matches


class RulesCollection(object):

    def __init__(self, rulesdirs=None):
        if rulesdirs is None:
            rulesdirs = []
        self.rulesdirs = ansiblelint.utils.expand_paths_vars(rulesdirs)
        self.rules = []
        for rulesdir in self.rulesdirs:
            self.extend(ansiblelint.utils.load_plugins(rulesdir))

    def register(self, obj):
        self.rules.append(obj)

    def __iter__(self):
        return iter(self.rules)

    def __len__(self):
        return len(self.rules)

    def extend(self, more):
        self.rules.extend(more)

    def run(self, playbookfile, tags=set(), skip_list=frozenset()):
        text = ""
        matches = list()

        try:
            with codecs.open(playbookfile['path'], mode='rb', encoding='utf-8') as f:
                text = f.read()
        except IOError as e:
            print("WARNING: Couldn't open %s - %s" %
                  (playbookfile['path'], e.strerror),
                  file=sys.stderr)
            return matches

        for rule in self.rules:
            if not tags or not set(rule.tags).union([rule.id]).isdisjoint(tags):
                rule_definition = set(rule.tags)
                rule_definition.add(rule.id)
                if set(rule_definition).isdisjoint(skip_list):
                    matches.extend(rule.matchlines(playbookfile, text))
                    matches.extend(rule.matchtasks(playbookfile, text))
                    matches.extend(rule.matchyaml(playbookfile, text))

        return matches

    def __repr__(self):
        return "\n".join([rule.verbose()
                          for rule in sorted(self.rules, key=lambda x: x.id)])

    def listtags(self):
        tags = defaultdict(list)
        for rule in self.rules:
            for tag in rule.tags:
                tags[tag].append("[{0}]".format(rule.id))
        results = []
        for tag in sorted(tags):
            results.append("{0} {1}".format(tag, tags[tag]))
        return "\n".join(results)


class Match(object):

    def __init__(self, linenumber, line, filename, rule, message=None):
        self.linenumber = linenumber
        self.line = line
        self.filename = filename
        self.rule = rule
        self.message = message or rule.shortdesc

    def __repr__(self):
        formatstr = u"[{0}] ({1}) matched {2}:{3} {4}"
        return formatstr.format(self.rule.id, self.message,
                                self.filename, self.linenumber, self.line)


class Runner(object):

    def __init__(self, rules, playbook, tags, skip_list, exclude_paths,
                 verbosity=0, checked_files=None):
        self.rules = rules
        self.playbooks = set()
        # assume role if directory
        if os.path.isdir(playbook):
            self.playbooks.add((os.path.join(playbook, ''), 'role'))
            self.playbook_dir = playbook
        else:
            self.playbooks.add((playbook, 'playbook'))
            self.playbook_dir = os.path.dirname(playbook)
        self.tags = tags
        self.skip_list = skip_list
        self._update_exclude_paths(exclude_paths)
        self.verbosity = verbosity
        if checked_files is None:
            checked_files = set()
        self.checked_files = checked_files

    def _update_exclude_paths(self, exclude_paths):
        if exclude_paths:
            # These will be (potentially) relative paths
            paths = ansiblelint.utils.expand_paths_vars(exclude_paths)
            # Since ansiblelint.utils.find_children returns absolute paths,
            # and the list of files we create in `Runner.run` can contain both
            # relative and absolute paths, we need to cover both bases.
            self.exclude_paths = paths + [os.path.abspath(p) for p in paths]
        else:
            self.exclude_paths = []

    def is_excluded(self, file_path):
        # Any will short-circuit as soon as something returns True, but will
        # be poor performance for the case where the path under question is
        # not excluded.
        return any(file_path.startswith(path) for path in self.exclude_paths)

    def run(self):
        files = list()
        for playbook in self.playbooks:
            if self.is_excluded(playbook[0]):
                continue
            if playbook[1] == 'role':
                continue
            files.append({'path': ansiblelint.utils.normpath(playbook[0]),
                          'type': playbook[1],
                          # add an absolute path here, so rules are able to validate if
                          # referenced files exist
                          'absolute_directory': os.path.dirname(playbook[0])})
        visited = set()
        while (visited != self.playbooks):
            for arg in self.playbooks - visited:
                for child in ansiblelint.utils.find_children(arg, self.playbook_dir):
                    if self.is_excluded(child['path']):
                        continue
                    self.playbooks.add((child['path'], child['type']))
                    files.append(child)
                visited.add(arg)

        matches = list()

        # remove duplicates from files list
        files = [value for n, value in enumerate(files) if value not in files[:n]]

        # remove files that have already been checked
        files = [x for x in files if x['path'] not in self.checked_files]
        for file in files:
            if self.verbosity > 0:
                print("Examining %s of type %s" % (
                    ansiblelint.utils.normpath(file['path']),
                    file['type']))
            matches.extend(self.rules.run(file, tags=set(self.tags),
                           skip_list=self.skip_list))
        # update list of checked files
        self.checked_files.update([x['path'] for x in files])

        return matches
