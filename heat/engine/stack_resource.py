# vim: tabstop=4 shiftwidth=4 softtabstop=4

#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo.config import cfg

from heat.common import exception
from heat.engine import attributes
from heat.engine import environment
from heat.engine import parser
from heat.engine import resource
from heat.engine import scheduler
from heat.engine import template as tmpl

from heat.openstack.common import log as logging
from heat.openstack.common.gettextutils import _

logger = logging.getLogger(__name__)


class StackResource(resource.Resource):
    '''
    An abstract Resource subclass that allows the management of an entire Stack
    as a resource in a parent stack.
    '''

    # Assume True as this is evaluated before the stack is created
    # so there is no way to know for sure without subclass-specific
    # template parsing.
    requires_deferred_auth = True

    def __init__(self, name, json_snippet, stack):
        super(StackResource, self).__init__(name, json_snippet, stack)
        self._nested = None
        if self.stack.parent_resource:
            self.recursion_depth = (
                self.stack.parent_resource.recursion_depth + 1)
        else:
            self.recursion_depth = 0

    def _outputs_to_attribs(self, json_snippet):
        if not self.attributes and 'Outputs' in json_snippet:
            self.attributes_schema = (
                attributes.Attributes
                .schema_from_outputs(json_snippet.get('Outputs')))
            self.attributes = attributes.Attributes(self.name,
                                                    self.attributes_schema,
                                                    self._resolve_attribute)

    def nested(self):
        '''
        Return a Stack object representing the nested (child) stack.
        '''
        if self._nested is None and self.resource_id is not None:
            self._nested = parser.Stack.load(self.context,
                                             self.resource_id,
                                             parent_resource=self,
                                             show_deleted=False)

            if self._nested is None:
                raise exception.NotFound(_('Nested stack not found in DB'))

        return self._nested

    def create_with_template(self, child_template, user_params,
                             timeout_mins=None):
        '''
        Handle the creation of the nested stack from a given JSON template.
        '''
        if self.recursion_depth >= cfg.CONF.max_nested_stack_depth:
            msg = _("Recursion depth exceeds %d.") % \
                cfg.CONF.max_nested_stack_depth
            raise exception.RequestLimitExceeded(message=msg)
        template = parser.Template(child_template)
        if ((len(template[tmpl.RESOURCES]) +
             self.stack.root_stack.total_resources() >
             cfg.CONF.max_resources_per_stack)):
            raise exception.RequestLimitExceeded(
                message=exception.StackResourceLimitExceeded.msg_fmt)
        self._outputs_to_attribs(child_template)

        # Note we disable rollback for nested stacks, since they
        # should be rolled back by the parent stack on failure
        nested = parser.Stack(self.context,
                              self.physical_resource_name(),
                              template,
                              environment.Environment(user_params),
                              timeout_mins=timeout_mins,
                              disable_rollback=True,
                              parent_resource=self,
                              owner_id=self.stack.id)
        nested.validate()
        self._nested = nested
        nested_id = self._nested.store()
        self.resource_id_set(nested_id)

        stack_creator = scheduler.TaskRunner(self._nested.stack_task,
                                             action=self._nested.CREATE)
        stack_creator.start(timeout=self._nested.timeout_secs())
        return stack_creator

    def check_create_complete(self, stack_creator):
        done = stack_creator.step()
        if done:
            if self._nested.state != (self._nested.CREATE,
                                      self._nested.COMPLETE):
                raise exception.Error(self._nested.status_reason)

        return done

    def update_with_template(self, child_template, user_params,
                             timeout_mins=None):
        """Update the nested stack with the new template."""
        template = parser.Template(child_template)
        # Note that there is no call to self._outputs_to_attribs here.
        # If we have a use case for updating attributes of the resource based
        # on updated templates we should make sure it's optional because not
        # all subclasses want that behavior, since they may offer custom
        # attributes.
        nested_stack = self.nested()
        if nested_stack is None:
            raise exception.Error(_('Cannot update %s, stack not created')
                                  % self.name)
        res_diff = (
            len(template[tmpl.RESOURCES]) - len(nested_stack.resources))
        new_size = nested_stack.root_stack.total_resources() + res_diff
        if new_size > cfg.CONF.max_resources_per_stack:
            raise exception.RequestLimitExceeded(
                message=exception.StackResourceLimitExceeded.msg_fmt)

        # Note we disable rollback for nested stacks, since they
        # should be rolled back by the parent stack on failure
        stack = parser.Stack(self.context,
                             self.physical_resource_name(),
                             template,
                             environment.Environment(user_params),
                             timeout_mins=timeout_mins,
                             disable_rollback=True,
                             parent_resource=self,
                             owner_id=self.stack.id)
        stack.validate()

        if not hasattr(type(self), 'attributes_schema'):
            self.attributes = None
            self._outputs_to_attribs(child_template)

        updater = scheduler.TaskRunner(nested_stack.update_task, stack)
        updater.start()
        return updater

    def check_update_complete(self, updater):
        if updater is None:
            return True

        if not updater.step():
            return False

        nested_stack = self.nested()
        if nested_stack.state != (nested_stack.UPDATE,
                                  nested_stack.COMPLETE):
            raise exception.Error(_("Nested stack update failed: %s") %
                                  nested_stack.status_reason)
        return True

    def delete_nested(self):
        '''
        Delete the nested stack.
        '''
        try:
            stack = self.nested()
        except exception.NotFound:
            logger.info(_("Stack not found to delete"))
        else:
            if stack is not None:
                delete_task = scheduler.TaskRunner(stack.delete)
                delete_task.start()
                return delete_task

    def check_delete_complete(self, delete_task):
        if delete_task is None:
            return True

        done = delete_task.step()
        if done:
            nested_stack = self.nested()
            if nested_stack.state != (nested_stack.DELETE,
                                      nested_stack.COMPLETE):
                raise exception.Error(nested_stack.status_reason)

        return done

    def handle_suspend(self):
        stack = self.nested()
        if stack is None:
            raise exception.Error(_('Cannot suspend %s, stack not created')
                                  % self.name)

        suspend_task = scheduler.TaskRunner(self._nested.stack_task,
                                            action=self._nested.SUSPEND,
                                            reverse=True)

        suspend_task.start(timeout=self._nested.timeout_secs())
        return suspend_task

    def check_suspend_complete(self, suspend_task):
        done = suspend_task.step()
        if done:
            if self._nested.state != (self._nested.SUSPEND,
                                      self._nested.COMPLETE):
                raise exception.Error(self._nested.status_reason)

        return done

    def handle_resume(self):
        stack = self.nested()
        if stack is None:
            raise exception.Error(_('Cannot resume %s, stack not created')
                                  % self.name)

        resume_task = scheduler.TaskRunner(self._nested.stack_task,
                                           action=self._nested.RESUME,
                                           reverse=False)

        resume_task.start(timeout=self._nested.timeout_secs())
        return resume_task

    def check_resume_complete(self, resume_task):
        done = resume_task.step()
        if done:
            if self._nested.state != (self._nested.RESUME,
                                      self._nested.COMPLETE):
                raise exception.Error(self._nested.status_reason)

        return done

    def set_deletion_policy(self, policy):
        self.nested().set_deletion_policy(policy)

    def get_abandon_data(self):
        return self.nested().get_abandon_data()

    def get_output(self, op):
        '''
        Return the specified Output value from the nested stack.

        If the output key does not exist, raise an InvalidTemplateAttribute
        exception.
        '''
        stack = self.nested()
        if stack is None:
            return None
        if op not in stack.outputs:
            raise exception.InvalidTemplateAttribute(resource=self.name,
                                                     key=op)
        return stack.output(op)

    def _resolve_attribute(self, name):
        return unicode(self.get_output(name))
