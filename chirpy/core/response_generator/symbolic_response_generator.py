from chirpy.core.callables import NamedCallable
from chirpy.core.state_manager import StateManager
from chirpy.core.regex import response_lists
from chirpy.core.response_generator.response_type import *
from chirpy.core.response_generator.neural_helpers import is_two_part, NEURAL_DECODE_CONFIG, get_random_fallback_neural_response
from chirpy.core.response_generator.state import NO_UPDATE, BaseSymbolicState, BaseSymbolicConditionalState
from chirpy.core.response_generator.neural_helpers import get_neural_fallback_handoff, neural_response_filtering
from chirpy.core.response_generator.treelet import Treelet
from chirpy.core.response_generator_datatypes import ResponseGeneratorResult, PromptResult, emptyResult, \
    emptyResult_with_conditional_state, emptyPrompt, UpdateEntity, AnswerType
from chirpy.core.response_generator.helpers import *
from chirpy.core.response_generator.response_generator import ResponseGenerator
from chirpy.core.response_generator.supernode import Supernode, Subnode
from chirpy.core.response_priority import ResponsePriority
from chirpy.core.response_generator.nlu import get_default_flags
from chirpy.symbolic_rgs import global_nlu
from chirpy.core.util import load_text_file
from typing import Set, Optional, List, Dict
import logging
import os

from importlib import import_module

from concurrent import futures

logger = logging.getLogger('chirpylogger')

import os
STOPWORDS_FILEPATH = os.path.join(os.path.dirname(__file__), '../../data/long_stopwords.txt')
STOPWORDS = load_text_file(STOPWORDS_FILEPATH)


def get_supernode_paths():
    path = os.path.join(os.path.dirname(__file__), '../../symbolic_rgs/active_supernodes.list')
    with open(path, 'r') as f:
        out = [x.strip() for x in f]
    out = [x for x in out if not x.startswith('#')]
    out = [x for x in out if x]
    return out


class SymbolicResponseGenerator(ResponseGenerator):
    name='SYMBOLIC_RESPONSE'
    def __init__(self,
                 state_manager,
                 supernode_paths=None,
                 ):

        super().__init__(state_manager,  
            can_give_prompts=True,
            state_constructor=BaseSymbolicState,
            conditional_state_constructor=BaseSymbolicConditionalState,
        )
        
        if supernode_paths is None:
            supernode_paths = get_supernode_paths()
        
        logger.warning(f"Starting load process with supernodes {supernode_paths}.")
        self.paths_to_supernodes = self.load_supernodes_from_paths(supernode_paths)
        logger.warning(f"Supernodes are: {', '.join(str(x) for x in self.paths_to_supernodes.keys())}")
                
    def load_supernodes_from_paths(self, supernode_paths):
        return {path: Supernode(path) for path in supernode_paths}
        
    def get_global_flags(self, state, utterance):
        # response types
        global_flags = {"GlobalFlag__" + k.name: v for k, v in global_response_type_dict(self, utterance).items()} 

        # map from string to None / template
        abrupt_initiative_templates = {
            "weather": WeatherTemplate(),   # problems
            # "time": 
            "repeat": SayThatAgainTemplate(),
            # "correct_name":
            "request_name": RequestNameTemplate(),
            # "age"
            # "clarification"
            # "abilities"
            # "personal"
            # "interrupt"
            "chatty": ChattyTemplate(),
            # "story"
            # "personal_problem"
            # "anything"
        }

        global_flags.update({f"GlobalFlag__Initiative__{k}": bool(v.execute(utterance)) for k, v in abrupt_initiative_templates.items()})
        logger.warning(f"GLOBAL FLAGS: {global_flags}")
        
        global_flags.update(global_nlu.get_flags(self, state, utterance))
        
        logger.warning(f"GlobalFlags are: {global_flags}")
        
        return global_flags
        
    def get_supernodes(self):
        return self.paths_to_supernodes.values()
        
    def get_next_supernode(self, python_context, contexts):
        can_start_supernodes = {supernode: supernode.can_start(python_context, contexts, return_specificity=True)
                                for supernode in self.get_supernodes()}
        can_start_supernodes = sorted(can_start_supernodes.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True)
        logger.warning(f"Supernodes that can start (in order): {can_start_supernodes}")
        return can_start_supernodes[0][0]

    def get_any_takeover_supernode(self, python_context, contexts):
        return self.paths_to_supernodes['GLOBALS']
        
    def get_takeover_or_current_supernode(self, state):
        """Returns a takeover supernode if one has high priority.
        Else, returns the current supernode if it exists."""
        
        # TODO: implement takeover supernode logic
        path = state.cur_supernode or 'GLOBALS'
        return self.paths_to_supernodes[path]
                
    def update_context(
        self,
        update_dict, 
        flags, 
        state_update_dict
    ):
        for value_name, value in update_dict.items():
            assert value_name.count('.') == 1, "Must have a namespace and a var name."
            namespace_name, value_name = value_name.split('.')
            assert namespace_name in ['flags', 'state'], f"Can't update namespace {namespace_name}"
            
            if namespace_name == 'flags':
                flags[value_name] = value
            else:
                state_update_dict[value_name] = value
                
    def get_response(self, state) -> ResponseGeneratorResult:
        logger.warning("Begin response for SymbolicResponseGenerator.")
        
        # Legacy response types
        
        self.state = state
        
        state, utterance, response_types = self.get_state_utterance_response_types()
        needs_prompt = False
        
        # figure out what supernode we're in
        supernode = self.get_takeover_or_current_supernode(state)
        
        logger.warning(f"Currently, we are in supernode {supernode}.")
            
        python_context = get_context_for_supernode(supernode)
        python_context.update({
            'rg': self,
            'supernode': supernode,
            'state': state
        })
        
        # Process locals        
        utilities = {
            "last_utterance": self.get_last_response().text, 
            "cur_entity": self.get_current_entity(),
            "cur_entity_name": self.get_current_entity().name if self.get_current_entity() else "",
            "cur_entity_name_lower": self.get_current_entity().name.lower() if self.get_current_entity() else "",
            "cur_talkable": self.get_current_entity().talkable_name if self.get_current_entity() else "",
            "cur_entity_talkable_lower": self.get_current_entity().talkable_name.lower() if self.get_current_entity() else "",
        }
        logging.warning(f"Utilities are: {utilities}")

        # perform nlu
        
        global_flags = self.get_global_flags(state, utterance)
        
        while True:
            flags = get_default_flags()
            flags.update(global_flags)
            flags.update(supernode.get_flags(self, state, utterance))
            
            logging.warning(f"Flags for supernode {supernode} are: {flags}")
            
            contexts = {
                'flags': flags,
                'state': state,
                'utilities': utilities,
            }
            
            if not supernode.can_continue(python_context, contexts):
                supernode = self.get_any_takeover_supernode(python_context, contexts)
                logger.warning(f"Switching to supernode {supernode}")
                continue
                
            locals = supernode.evaluate_locals(python_context, contexts)
            contexts['locals'] = locals
            logger.warning(f"Finished evaluating locals: {'; '.join((k + ': ' + v) for (k, v) in locals.items())}")
            locals['cur_entity'] = self.get_current_entity()
            break
            
        conditional_state_updates = {}
        self.update_context(supernode.get_state_updates(python_context, contexts),
                            flags,
                            conditional_state_updates)
        state.update(conditional_state_updates)

        # select subnode
        subnode = supernode.get_optimal_subnode(python_context, contexts)
        response = subnode.get_response(python_context, contexts)
        logger.warning(f'Received {response} from subnode {subnode}.')
        assert response is not None, "Received a None response."

        # update state
        conditional_state_updates = {}
        self.update_context(supernode.get_state_updates_after(python_context, contexts),
                            flags,
                            conditional_state_updates)
        self.update_context(subnode.get_state_updates(python_context, contexts),
                            flags,
                            conditional_state_updates)
        
        state.update(conditional_state_updates)
        
        logger.warning(f"Conditional state updates are {conditional_state_updates}, state is {state}")
        
        # get next prompt
        next_supernode = self.get_next_supernode(python_context, contexts)
        prompt = next_supernode.get_optimal_prompt(python_context, contexts) # TODO fix contexts

        print(f"OPTIMAL PROMPT: {prompt}")
        
        conditional_state = BaseSymbolicConditionalState(
            data=state.data,
            cur_supernode=next_supernode.name,                                                    
        )
    
        # TODO
        answer_type = AnswerType.QUESTION_SELFHANDLING
        
        return ResponseGeneratorResult(text=response + " " + prompt,
                                       priority=ResponsePriority.STRONG_CONTINUE, 
                                       needs_prompt=False,
                                       state=state,
                                       cur_entity=None, 
                                       answer_type=answer_type,
                                       conditional_state=conditional_state
                                      )
        

    def update_state_if_chosen(self, state, conditional_state):
        if conditional_state is None: return state

        state.cur_supernode = conditional_state.cur_supernode
        state.data.update(conditional_state.data)
        
        return state

    def update_state_if_not_chosen(self, state, conditional_state):
        """
        By default, this sets the prev_treelet_str and next_treelet_str to '' and resets num_turns_in_rg to 0.
        Response types are also saved.
        No other attributes are updated.
        All other attributes in ConditionalState are set to NO-UPDATE
        """
        response_types = self.get_cache(f'{self.name}_response_types')
        if response_types is not None:
            state.response_types = construct_response_types_tuple(response_types)

        state.prev_treelet_str = ''
        state.next_treelet_str = ''
        state.num_turns_in_rg = 0

        return state

    def set_user_attribute(self, attr_name, value):
        setattr(self.state_manager.user_attributes, attr_name, value)

    def get_user_attribute(self, attr_name, default):
        return getattr(self.state_manager.user_attributes, attr_name, default)