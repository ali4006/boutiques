#!/usr/bin/env python

from argparse import ArgumentParser
from jsonschema import ValidationError
from boutiques.validator import validate_descriptor
from boutiques.util.utils import loadJson
from boutiques.logger import raise_error
import boutiques
import yaml
import simplejson as json
import os
import os.path as op
import re
import sys
from docopt import parse_defaults, parse_pattern, parse_argv
from docopt import formal_usage, DocoptLanguageError
from docopt import AnyOptions, TokenStream, Option, Argument, Command
import imp
import collections


class ImportError(Exception):
    pass


class Importer():

    def __init__(self, input_descriptor, output_descriptor,
                 input_invocation, output_invocation):
        self.input_descriptor = input_descriptor
        self.output_descriptor = output_descriptor
        self.input_invocation = input_invocation
        self.output_invocation = output_invocation

    def upgrade_04(self):
        """
         Differences between 0.4 and current (0.5):
           -schema version (obv)
           -singularity should now be represented same as docker
           -walltime should be part of suggested_resources structure

        I.e.
        "schema-version": "0.4",
                    ...... becomes.....
        "schema-version": "0.5",

        I.e.
        "container-image": {
          "type": "singularity",
          "url": "shub://gkiar/ndmg-cbrain:master"
          },
                    ...... becomes.....
        "container-image": {
          "type": "singularity",
          "image": "gkiar/ndmg-cbrain:master",
          "index": "shub://",
        },

        I.e.
        "walltime-estimate": 3600,
                    ...... becomes.....
        "suggested-resources": {
          "walltime-estimate": 3600
        },
        """
        descriptor = loadJson(self.input_descriptor)

        if descriptor["schema-version"] != "0.4":
            raise_error(ImportError, "The input descriptor must have "
                        "'schema-version'=0.4")
        descriptor["schema-version"] = "0.5"

        if "container-image" in descriptor.keys():
            if "singularity" == descriptor["container-image"]["type"]:
                url = descriptor["container-image"]["url"]
                img = url.split("://")
                if len(img) == 1:
                    descriptor["container-image"]["image"] = img[0]
                elif len(img) == 2:
                    descriptor["container-image"]["image"] = img[1]
                    descriptor["container-image"]["index"] = img[0] + "://"
                del descriptor["container-image"]["url"]
            elif ("docker" == descriptor["container-image"]["type"] and
                  descriptor["container-image"].get("index")):
                url = descriptor["container-image"]["index"].split("://")[-1]
                descriptor["container-image"]["index"] = url

        if "walltime-estimate" in descriptor.keys():
            descriptor["suggested-resources"] =\
              {"walltime-estimate": descriptor["walltime-estimate"]}
            del descriptor["walltime-estimate"]

        with open(self.output_descriptor, 'w') as fhandle:
            fhandle.write(json.dumps(descriptor, indent=4, sort_keys=True))
        validate_descriptor(self.output_descriptor)

    def get_entry_point(self, input_descriptor):
        entrypoint = None
        with open(os.path.join(self.input_descriptor, "Dockerfile")) as f:
            content = f.readlines()
        for line in content:
            split = line.split()
            if len(split) >= 2 and split[0] == "ENTRYPOINT":
                entrypoint = split[1].strip("[]\"")
        return entrypoint

    def import_docopt(self):
        path, fil = op.split(__file__)
        template_file = op.join(
            path, "templates", "template_docopt_descriptor.json")
        docstring = imp.load_source(
            'docopt_pyscript', self.input_descriptor).__doc__

        dcptImptr = Docopt_Importer(docstring, template_file)
        # The order matters
        dcptImptr.loadDocoptDescription()
        dcptImptr.loadDescriptionAndType()
        dcptImptr.generateInputsAndCommandLine(dcptImptr.pattern)
        # for root in dcptImptr.dependencies:
        #    dcptImptr._printNodes(dcptImptr.dependencies[root], 0)
        dcptImptr.addInputsRecursive(dcptImptr.dependencies)
        dcptImptr._removeGroupsFromRequires()

        with open(self.output_descriptor, "w") as output:
            output.write(json.dumps(dcptImptr.descriptor, indent=4))

    def import_bids(self):
        path, fil = os.path.split(__file__)
        template_file = os.path.join(path, "templates", "bids-app.json")

        with open(template_file) as f:
            template_string = f.read()

        errors = []
        app_name = os.path.basename(os.path.abspath(self.input_descriptor))
        version = 'unknown'
        version_file = os.path.join(self.input_descriptor, "version")
        if os.path.exists(version_file):
            with open(version_file, "r") as f:
                version = f.read().strip()
        git_repo = "https://github.com/BIDS-Apps/"+app_name
        entrypoint = self.get_entry_point(self.input_descriptor)
        container_image = "bids/"+app_name
        analysis_types = "participant\", \"group\", \"session"

        if not entrypoint:
            errors.append("No entrypoint found in container.")

        if len(errors):
            raise_error(ValidationError, "Invalid descriptor:\n"+"\n".
                        join(errors))

        template_string = template_string.replace("@@APP_NAME@@", app_name)
        template_string = template_string.replace("@@VERSION@@", version)
        template_string = template_string.replace("@@GIT_REPO_URL@@", git_repo)
        template_string = template_string.replace("@@DOCKER_ENTRYPOINT@@",
                                                  entrypoint)
        template_string = template_string.replace("@@CONTAINER_IMAGE@@",
                                                  container_image)
        template_string = template_string.replace("@@ANALYSIS_TYPES@@",
                                                  analysis_types)

        with open(self.output_descriptor, "w") as f:
            f.write(template_string)

    def import_cwl(self):

        # Read the CWL descriptor
        with open(self.input_descriptor, 'r') as f:
            cwl_desc = yaml.load(f)

        # validate yaml descriptor?

        bout_desc = {}
        # Command line
        if cwl_desc.get('baseCommand') is None:
            raise_error(ImportError, 'Cannot find baseCommand attribute, '
                        'perhaps you passed a workflow document, '
                        'this is not supported')
        if type(cwl_desc['baseCommand']) is list:
            command_line = ""
            for i in cwl_desc['baseCommand']:
                command_line += i+" "
        else:
            command_line = cwl_desc['baseCommand']
        if cwl_desc.get('arguments'):
            for i in cwl_desc['arguments']:
                if type(i) is dict:
                    raise_error(ImportError, 'Dict arguments not supported.')
                if "$(runtime." in i:
                    raise_error(ImportError, 'Runtime parameters '
                                ' are not supported:'
                                " "+i)
                command_line += i+" "
        boutiques_inputs = []

        # Inputs
        def position(x):
            if (type(x) is dict and
                    x.get('inputBinding') and
                    x['inputBinding'].get('position')):
                return x['inputBinding']['position']
            return 0
        sorted_inputs = sorted(cwl_desc['inputs'],
                               key=lambda x: (position(cwl_desc['inputs'][x])))
        # Mapping between CQL and Boutiques input types
        boutiques_types = {
          'string': 'String',
          'File': 'File',
          'File?': 'File',
          'boolean': 'Flag',
          'int': 'Number'
          # float type?
        }
        for cwl_input in sorted_inputs:
            bout_input = {}
            # Easy stuff
            bout_input['id'] = cwl_input  # perhaps 'idify' that
            cwl_in_obj = cwl_desc['inputs'][cwl_input]
            if type(cwl_in_obj) is dict and cwl_in_obj.get('name'):
                bout_input['name'] = cwl_in_obj['name']
            else:
                bout_input['name'] = cwl_input
            value_key = "[{0}]".format(cwl_input.upper())
            if (type(cwl_in_obj) is dict and
                    cwl_in_obj.get('inputBinding') is not None):
                command_line += " "+value_key
            bout_input['value-key'] = value_key

            # CWL type parsing
            if type(cwl_in_obj) is dict:
                cwl_type = cwl_in_obj['type']
            else:
                cwl_type = 'string'
            if type(cwl_type) is dict:  # It must be an array
                if cwl_type['type'] != "array":
                    raise_error(ImportError, "Only 1-level nested "
                                "types of type"
                                " 'array' are supported (CWL input: {0})".
                                format(cwl_input))
                if cwl_type.get('inputBinding') is not None:
                    raise_error(ImportError, "Input bindings of "
                                "array elements "
                                "are not supported (CWL input: {0})".
                                format(cwl_input))
                cwl_type = cwl_type['items']
                bout_input['list'] = True
            if type(cwl_type) != str:
                raise_error(ImportError, "Unknown type:"
                            " {0}".format(str(cwl_type)))
            boutiques_type = boutiques_types[cwl_type.replace("[]", "")
                                                     .replace("?", "")]
            bout_input['type'] = boutiques_type
            if cwl_type == 'int':
                bout_input['integer'] = True
            if '?' in cwl_type or boutiques_type == "Flag":
                bout_input['optional'] = True

            # CWL input binding
            if type(cwl_in_obj) is dict:
                cwl_input_binding = cwl_in_obj['inputBinding']
            else:
                cwl_input_binding = {}
            if cwl_input_binding.get('prefix'):
                bout_input['command-line-flag'] = cwl_input_binding['prefix']
                if (not (cwl_input_binding.get('separate') is None) and
                        cwl_input_binding['separate'] is False):
                    bout_input['command-line-flag-separator'] = ''
            boutiques_inputs.append(bout_input)

            # array types
            if cwl_type.endswith("[]"):
                bout_input['list'] = True
                if cwl_input_binding.get("itemSeparator"):
                    if cwl_input_binding['itemSeparator'] != ' ':
                        raise_error(ImportError, 'Array separators wont be '
                                    'supported until #76 is implemented')

        # Outputs

        def resolve_glob(glob, boutiques_inputs):
            if not glob.startswith("$"):
                return glob
            if not glob.startswith("$(inputs."):
                raise_error(ImportError, "Unsupported reference: "+glob)
            input_id = glob.replace("$(inputs.", "").replace(")", "")
            for i in boutiques_inputs:
                if i['id'] == input_id:
                    return i['value-key']
            raise_error(ImportError, "Unresolved reference"
                        " in glob: " + glob)

        boutiques_outputs = []
        sorted_outputs = sorted(cwl_desc['outputs'],
                                key=(lambda x: cwl_desc['outputs'][x].
                                     get('outputBinding')))
        for cwl_output in sorted_outputs:
            bout_output = {}
            bout_output['id'] = cwl_output  # perhaps 'idify' that
            if cwl_desc['outputs'][cwl_output].get('name'):
                bout_output['name'] = cwl_desc['outputs'][cwl_output]['name']
            else:
                bout_output['name'] = cwl_output
            cwl_out_binding = (cwl_desc['outputs'][cwl_output].
                               get('outputBinding'))
            if cwl_out_binding and cwl_out_binding.get('glob'):
                glob = cwl_out_binding['glob']
                bout_output['path-template'] = resolve_glob(glob,
                                                            boutiques_inputs)
                cwl_out_obj = cwl_desc['outputs'][cwl_output]
                if type(cwl_out_obj.get('type')) is dict:
                    if (cwl_out_obj['type'].get('type') and
                       cwl_out_obj['type']['type'] == 'array'):
                        bout_output['list'] = True
                    else:
                        raise_error(ImportError, 'Unsupported output type: ' +
                                    cwl_output['type'])
                boutiques_outputs.append(bout_output)
        # Boutiques descriptors have to have at least 1 output file
        if len(boutiques_outputs) == 0 or cwl_desc.get('stdout'):
            stdout = cwl_desc.get('stdout') or 'stdout.txt'
            command_line += " > "+stdout
            boutiques_outputs.append(
              {
                  'id': 'stdout',
                  'name': 'Standard output',
                  'path-template': 'stdout.txt'
              }
            )

        # Mandatory boutiques fields
        bout_desc['command-line'] = command_line
        if cwl_desc.get("doc"):
            bout_desc['description'] = (cwl_desc.get("doc").
                                        replace(os.linesep, ''))
        else:
            bout_desc['description'] = "Tool imported from CWL."
        bout_desc['inputs'] = boutiques_inputs
        # This may not be a great idea but not sure if CWL tools have names
        bout_desc['name'] = op.splitext(op.basename(self.input_descriptor))[0]
        bout_desc['output-files'] = boutiques_outputs
        bout_desc['schema-version'] = '0.5'
        bout_desc['tool-version'] = "unknown"  # perhaphs there's one in cwl

        # Hints and requirements
        def parse_req(req, req_type, bout_desc):
            # We could support InitialWorkDirRequiment, through config files
            if req_type == 'DockerRequirement':
                container_image = {}
                container_image['type'] = 'docker'
                container_image['index'] = 'index.docker.io'
                container_image['image'] = req['dockerPull']
                bout_desc['container-image'] = container_image
                return
            if req_type == 'EnvVarRequirement':
                bout_envars = []
                for env_var in req['envDef']:
                    bout_env_var = {}
                    bout_env_var['name'] = env_var
                    bout_env_var['value'] = resolve_glob(
                                              req['envDef'][env_var],
                                              boutiques_inputs)
                    bout_envars.append(bout_env_var)
                    bout_desc['environment-variables'] = bout_envars
                return
            if req_type == 'ResourceRequirement':
                suggested_resources = {}
                if req.get('ramMin'):
                    suggested_resources['ram'] = req['ramMin']
                if req.get('coresMin'):
                    suggeseted_resources['cpu-cores'] = req['coresMin']
                bout_desc['suggested-resources'] = suggested_resources
                return
            if req_type == 'InitialWorkDirRequirement':
                listing = req.get('listing')
                for entry in listing:
                    file_name = entry.get('entryname')
                    assert(file_name is not None)
                    template = entry.get('entry')
                    for i in boutiques_inputs:
                        if i.get("value-key"):
                            template = template.replace("$(inputs."+i['id']+")",
                                                        i.get("value-key"))
                    template = template.split(os.linesep)
                    assert(template is not None)
                    name = op.splitext(file_name)[0]
                    boutiques_outputs.append(
                        {
                            'id': name,
                            'name': name,
                            'path-template': file_name,
                            'file-template': template
                        })
                return
            raise_error(ImportError, 'Unsupported requirement: '+str(req))

        for key in ['requirements', 'hints']:
            if(cwl_desc.get(key)):
                for i in cwl_desc[key]:
                    parse_req(cwl_desc[key][i], i, bout_desc)

        # enum types?

        # Write descriptor
        with open(self.output_descriptor, 'w') as f:
            f.write(json.dumps(bout_desc, indent=4, sort_keys=True))
        validate_descriptor(self.output_descriptor)

        if self.input_invocation is None:
            return

        # Convert invocation
        def get_input(descriptor_inputs, input_id):
            for inp in descriptor_inputs:
                if inp['id'] == input_id:
                    return inp
            return False
        boutiques_invocation = {}
        with open(self.input_invocation, 'r') as f:
            cwl_inputs = yaml.load(f)
        for input_name in cwl_inputs:
            if get_input(bout_desc['inputs'], input_name)['type'] != "File":
                input_value = cwl_inputs[input_name]
            else:
                input_value = cwl_inputs[input_name]['path']
            boutiques_invocation[input_name] = input_value
        with open(self.output_invocation, 'w') as f:
            f.write(json.dumps(boutiques_invocation, indent=4, sort_keys=True))
        boutiques.invocation(self.output_descriptor,
                             "-i", self.output_invocation)


class Docopt_Importer():
    def __init__(self, docopt_str, base_descriptor):
        with open(base_descriptor, "r") as base_desc:
            self.descriptor = collections.OrderedDict(json.load(base_desc))

        self.docopt_str = docopt_str
        self.dependencies = collections.OrderedDict()
        self.all_desc_and_type = collections.OrderedDict()
        self.unique_ids = collections.OrderedDict()

        # try:
        # All native docopt code, should succeed if docopt script is valid
        options = parse_defaults(docopt_str)

        self.pattern = parse_pattern(
            formal_usage(self._parse_section('usage:', docopt_str)[0]),
            options)

        argv = parse_argv(
            TokenStream(sys.argv[1:], DocoptLanguageError),
            list(options), False)
        pattern_options = set(self.pattern.flat(Option))

        for options_shortcut in self.pattern.flat(AnyOptions):
            doc_options = parse_defaults(docopt_str)
            options_shortcut.children = list(
                set(doc_options) - pattern_options)
        matched, left, collected = self.pattern.fix().match(argv)
        # except Exception:
        #    raise_error(ImportError, "Invalid docopt script")

    def loadDocoptDescription(self):
        self.descriptor["description"] = self.docopt_str\
            .replace("".join(self._parse_section(
                'usage:', self.docopt_str)), "")\
            .replace("".join(self._parse_section(
                'arguments:', self.docopt_str)), "")\
            .replace("".join(self._parse_section(
                'options:', self.docopt_str)), "")\
            .replace("\n\n", "\n").strip()

    def loadDescriptionAndType(self):
        # using docopt code to extract description and type from args
        for line in (self._parse_section('arguments:', self.docopt_str) +
                     self._parse_section('options:', self.docopt_str)):
            _, _, s = line.partition(':')  # get rid of "options:"
            split = re.split(r'\n[ \t]*(-\S+?)', '\n' + s)[1:] if\
                line in self._parse_section('options:', self.docopt_str) else\
                re.split(r'\n[ \t]*(<\S+?)', '\n' + s)[1:]
            split = [s1 + s2 for s1, s2 in zip(split[::2], split[1::2])]
            # parse each line of Arguments and Options
            for arg_str in [s for s in split if (s.startswith('-') or
                                                 s.startswith('<'))]:
                arg = Option.parse(arg_str) if arg_str.startswith('-')\
                    else Argument.parse(arg_str)
                arg_segs = arg_str.partition('  ')
                self.all_desc_and_type[arg.name] = {
                    "desc": arg_segs[-1].replace('\n', ' ')
                                        .replace("  ", '').strip()}
                if hasattr(arg, "value") and arg.value is not None and\
                   arg.value is not False:
                    self.all_desc_and_type[arg.name]['default-value'] =\
                        arg.value
                if type(arg) is Option and arg.argcount > 0:
                    for typ in [seg for seg in arg_segs[0]
                                .replace(',', ' ')
                                .replace('=', ' ')
                                .split() if seg[0] != "-"]:
                        self.all_desc_and_type[arg.name]["type"] = typ

    def generateInputsAndCommandLine(self, node):
        child_node_type = type(node.children[0]).__name__
        if hasattr(node, 'children') and (child_node_type == "Either" or
                                          child_node_type == "Required"):
            for child in node.children:
                self.generateInputsAndCommandLine(child)
        # Traversing reached usage level
        else:
            self.descriptor['command-line'] = self._parse_section(
                'usage:', self.docopt_str)[0].split("\n")[1:][0].split()[0]
            self._loadInputsFromUsage(node)

    def addInputsRecursive(self, node, requires=[], s=0):
        args_id = list(node.keys())
        if len(args_id) == 1:
            if 'mutex_members' in node[args_id[0]]:
                for mutex_member in node[args_id[0]]['mutex_members']:
                    self._addInput(mutex_member, requires)
                self._addMutexGroup([member["name"] for member in
                                    node[args_id[0]]['mutex_members']])
            else:
                self._addInput(
                    node[args_id[0]],
                    requires,
                    isList=node[args_id[0]]["isList"] if 'isList' in
                    node[args_id[0]] else False)
            self.addInputsRecursive(
                node[args_id[0]]['children'], requires, s=s+2)
        elif len(args_id) > 1:
            mutex_names = []
            for name in args_id:
                if 'mutex_members' in node[name]:
                    for mutex_member in node[name]['mutex_members']:
                        self._addInput(mutex_member, requires)
                    self._addMutexGroup([member["name"] for member in
                                        node[name]['mutex_members']])
                else:
                    self._addInput(
                        node[name],
                        requires + self._getLineageChildren(node[name], []),
                        isList=node[name]["isList"] if 'isList' in
                        node[name] else False)
                self.addInputsRecursive(
                    node[name]['children'], [node[name]['name']], s=s+2)
                if not node[name]['optional']:
                    mutex_names.append(node[name]['name'])
            if len(mutex_names) > 1:
                self._addMutexGroup(mutex_names)

    def _loadInputsFromUsage(self, usage):
        ancestors = []
        for arg in usage.children:
            arg_type = type(arg).__name__
            if hasattr(arg, "children"):
                fchild_type = type(arg.children[0]).__name__
                # Has sub-arguments, maybe recurse into _loadRtrctnsFrmUsg
                # but have to deal with children in subtype
                if arg_type == "Optional" and fchild_type == "AnyOptions":
                    for option in arg.children[0].children:
                        self._addArgumentToDependencies(
                            option, ancestors=ancestors, optional=True)
                elif arg_type == "OneOrMore":
                    list_name = "<list_of_{0}>".format(
                        self._getParamName(arg.children[0].name))
                    list_arg = Argument(list_name)
                    list_arg.parse(list_name)
                    self.all_desc_and_type[list_name] = {
                        'desc': "List of {0}".format(
                            self._getParamName(arg.children[0].name))}
                    self._addArgumentToDependencies(
                        list_arg, ancestors=ancestors, isList=True)
                    ancestors.append(list_name)
                elif arg_type == "Optional" and fchild_type == "Option":
                    for option in arg.children:
                        self._addArgumentToDependencies(
                            option, ancestors=ancestors, optional=True)
                elif arg_type == "Optional" and fchild_type == "Either":
                    ancestors = self._addGroupArgumentToDependencies(
                        arg, ancestors, optional=True)
                elif arg_type == "Required" and fchild_type == "Either":
                    ancestors = self._addGroupArgumentToDependencies(
                        arg, ancestors)
            elif arg_type == "Command":
                self._addArgumentToDependencies(arg, ancestors=ancestors)
                ancestors.append(arg.name)
            elif arg_type == "Argument":
                self._addArgumentToDependencies(arg, ancestors=ancestors)
                ancestors.append(arg.name)
            elif arg_type == "Option":
                self._addArgumentToDependencies(
                    arg, ancestors=ancestors, optional=True)
                ancestors.append(arg.name)
            else:
                raise_error(
                    ImportError,
                    "Non implemented docopt arg.type: {0}".format(arg_type))

    def _addMutexGroup(self, arg_names):
        pretty_name = "_".join([self._getParamName(name)
                                for name in arg_names])
        unique_name = self._getUniqueId(pretty_name)
        new_group = {
            "id": pretty_name,
            "name": "Mutex group with members: {0}".format(", ".join(
                [self._getStrippedName(name) for name in arg_names])),
            "members": [self._getParamName(name) for name in arg_names],
            "mutually-exclusive": True
        }
        if "groups" not in self.descriptor:
            self.descriptor['groups'] = []
        self.descriptor['groups'].append(new_group)

    def _addGroupArgumentToDependencies(self, arg, ancestors, optional=False):
        options = arg.children[0].children
        p_node = self._getDependencyParentNode(ancestors)
        members = []
        for option in options:
            mutex_member = self._getDependencyArgument(option)
            members.append(mutex_member)

        pretty_names = [member["name"] for member in members]
        names = [arg.name for arg in arg.children[0].children]
        gdesc = "Group key for mutex choices: {0}".format(
            " and ".join(names))
        for name in names:
            if name in self.all_desc_and_type:
                gdesc = gdesc.replace(name, "{0} ({1})".format(
                    name, self.all_desc_and_type[name]['desc']
                ))
        gname = "<{0}>".format("_".join(pretty_names))
        grp_arg = Argument(gname)
        grp_arg.parse(gname)
        self.all_desc_and_type[gname] = {'desc': gdesc}

        self._addArgumentToDependencies(
            grp_arg, ancestors=ancestors,
            optional=optional, members=members)
        ancestors.append(grp_arg.name)
        return ancestors

    def _getDependencyArgument(self, node, isList=False,
                               optional=False, members=[]):
        new_arg = {
            "id": node.name,
            "name": self._getUniqueId(self._getParamName(node.name)),
            "desc": self.all_desc_and_type[node.name]['desc']
            if node.name in self.all_desc_and_type
            else "",
            "optional": optional,
            "parent": None,
            "children": collections.OrderedDict()}

        if new_arg is not None and isList:
            new_arg["isList"] = True
        if members != []:
            new_arg["mutex_members"] = members

        if node.name in self.all_desc_and_type:
            if 'type' in self.all_desc_and_type[node.name]:
                new_arg["type"] = self.all_desc_and_type[node.name]['type'] if\
                    self.all_desc_and_type[node.name]['type'] in\
                    {"File", "Flag", "Number", "String"} else "String"

            if 'default-value' in self.all_desc_and_type[node.name]:
                new_arg['default-value'] =\
                    self.all_desc_and_type[node.name]['default-value']
        if hasattr(node, 'long') and node.long is not None:
            # ensure flag has long hand flag
            new_arg["flag"] = node.long
        return new_arg

    def _addArgumentToDependencies(self, node, ancestors=None,
                                   isList=False, optional=False,
                                   members=[]):
        p_node = self._getDependencyParentNode(ancestors)
        argAdded = self._getDependencyArgument(node, isList, optional, members)

        if ancestors == [] and node.name not in self.dependencies:
            self.dependencies[node.name] = argAdded
        elif ancestors != [] and p_node is not None:
            argAdded["parent"] = p_node
            if node.name in p_node['children']:
                p_node['children'][node.name]['children'].update(
                    argAdded['children'])
            else:
                p_node['children'][node.name] = argAdded

        if hasattr(node, 'long') and node.long is not None:
            # ensure flag has long hand flag
            argAdded["flag"] = node.long
            if p_node is not None and 'flag' in p_node and\
               argAdded["flag"] == p_node["flag"]:
                # if parent and child are same option (therefore has short-hand)
                # create new input with short-hand flag
                self.dependencies[
                    p_node['children'][node.name]['name']] = {
                        'id': p_node['children'][node.name]['id'],
                        'name': p_node['children'][node.name]['name'],
                        'desc': p_node['children'][node.name]['desc'],
                        'flag': node.short,
                        'optional': p_node['children']
                                    [node.name]['optional'],
                        'parent': None,
                        'children': p_node['children']
                                    [node.name]['children']}
                del p_node['children'][node.name]

    def _addInput(self, arg, requires, isList=False):
        if "inputs" not in self.descriptor:
            self.descriptor['inputs'] = []

        param_key = arg["id"]
        param_name = arg["name"]
        new_inp = {
            "id": param_name.replace("-", "_"),
            "name": param_name.replace("_", " ").replace("-", " "),
            "description": arg['desc'],
            "optional": True,
            "value-key": "[{0}]".format(param_name).upper()
        }
        self.descriptor["command-line"] += " {0}".format(new_inp['value-key'])

        if requires != []:
            new_inp["requires-inputs"] = requires
        # Only add list param when isList
        if isList:
            new_inp['list'] = True
        if "default-value" in arg:
            new_inp['default-value'] = arg['default-value']
        if "flag" in arg:
            if "type" in arg:
                new_inp['type'] = arg['type']
            else:
                new_inp['type'] = "Flag"
            new_inp["command-line-flag"] = arg["flag"]
        else:
            new_inp['type'] = "String"
        if 'mutex_members' in arg:
            mutex_names = []
            for choice_key in arg['children']:
                choice = arg['children'][choice_key]['id']
                if choice in arg['mutex_members']:
                    mutex_names.append(choice_key)
            if len(mutex_names) > 1:
                self._addMutexGroup(mutex_names)
        else:
            self.descriptor['inputs'].append(new_inp)

    def _getLineageChildren(self, node, descendants):
        child_keys = list(node['children'].keys())
        if len(child_keys) == 1 and\
           not node['children'][child_keys[0]]['optional']:
            descendants.append(node['children'][child_keys[0]]['name'])
            self._getLineageChildren(
                node['children'][child_keys[0]], descendants)
        return descendants

    def _getDependencyParentNode(self, ancestors):
        last_node = None
        for ancestor in ancestors:
            if last_node is None:
                for root in self.dependencies:
                    if self.dependencies[root]['id'] == ancestor:
                        last_node = self.dependencies[root]
            elif ancestor in [last_node['children'][key]['id'] for
                              key in last_node['children']]:
                last_node = last_node['children'][
                    [{'id': last_node['children'][n]['id'], 'key': n} for
                     n in last_node['children']][-1]['key']]
            else:
                return None
        return last_node

    def _getUniqueId(self, name):
        id_count = 1
        while name + (str(id_count) if id_count > 1 else "") in self.unique_ids:
            id_count += 1
        new_unique_id = name + ("_" + str(id_count) if id_count > 1 else "")
        self.unique_ids[new_unique_id] = {
            'id': new_unique_id, 'original': name}
        return new_unique_id

    def _getStrippedName(self, name):
        # only parses --arg and <arg>
        if name[0] == "-" and name[1] == "-":
            return name[2:]
        elif name[0] == "<" and name[-1] == ">":
            return name[1:-1]
        else:
            return name

    def _getParamName(self, param):
        # returns descriptor id compliant name
        chars = ['<', '>', '[', ']', '(', ')']
        if param[0] == "-" and param[1] == "-":
            param = param[2:].replace('-', '_')
            for char in chars:
                param = param.replace(char, "")
        elif param[0] == "<" and param[-1] == ">":
            param = param[1:-1].replace('-', '_')
            for char in chars:
                param = param.replace(char, "")
        else:
            for char in chars:
                param = param.replace(char, "")
        return param

    def _parse_section(self, name, source):
        pattern = re.compile(
            '^([^\n]*' + name + '[^\n]*\n?(?:[ \t].*?(?:\n|$))*)',
            re.IGNORECASE | re.MULTILINE)
        return [s.strip() for s in pattern.findall(source)]

    def _printNodes(self, node, tabs):
        # print dependency tree
        print(" " * tabs + node["name"], end="")
        if "mutex_members" in node:
            print(", members:", end="")
            for member in node["mutex_members"]:
                print(" " + member["name"], end="")
        print()
        for child in node["children"]:
            self._printNodes(node["children"][child], tabs+1)

    def _removeGroupsFromRequires(self):
        if 'groups' in self.descriptor:
            gnames = [group['id'] for group in self.descriptor['groups']]
            for inp in self.descriptor['inputs']:
                if "requires-inputs" in inp:
                    new_requires = []
                    for requiree in inp["requires-inputs"]:
                        if requiree not in gnames:
                            new_requires.append(requiree)
                    inp["requires-inputs"] = new_requires
                    if inp["requires-inputs"] == []:
                        del inp["requires-inputs"]
