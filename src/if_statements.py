import queue
import typing
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Union

import attr

from error import DecompFailure
from flow_graph import (BasicNode, Block, ConditionalNode, FlowGraph, Node,
                        ReturnNode)
from options import Options
from translate import (BinaryOp, BlockInfo, Condition, FunctionInfo, Type,
                       as_type, simplify_condition, stringify_expr)


@attr.s
class Context:
    flow_graph: FlowGraph = attr.ib()
    options: Options = attr.ib()
    reachable_without: Dict[typing.Tuple[Node, Node, Node], bool] = attr.ib(factory=dict)
    return_type: Type = attr.ib(factory=Type.any)
    is_void: bool = attr.ib(default=True)
    goto_nodes: Set[Node] = attr.ib(factory=set)
    emitted_nodes: Set[Node] = attr.ib(factory=set)
    has_warned: bool = attr.ib(default=False)

@attr.s
class IfElseStatement:
    condition: Condition = attr.ib()
    indent: int = attr.ib()
    if_body: 'Body' = attr.ib()
    else_body: Optional['Body'] = attr.ib(default=None)

    def should_write(self) -> bool:
        return True

    def __str__(self) -> str:
        space = ' ' * self.indent
        # Avoid duplicate parentheses. TODO: make this cleaner and do it more
        # uniformly, not just here.
        condition = simplify_condition(self.condition)
        cond_str = stringify_expr(condition)
        if_str = '\n'.join([
            f'{space}if ({cond_str})',
            f'{space}{{',
            str(self.if_body),  # has its own indentation
            f'{space}}}',
        ])
        if self.else_body is not None:
            else_str = '\n'.join([
                f'{space}else',
                f'{space}{{',
                str(self.else_body),
                f'{space}}}',
            ])
            if_str = if_str + '\n' + else_str
        return if_str

@attr.s
class SimpleStatement:
    indent: int = attr.ib()
    contents: str = attr.ib()

    def should_write(self) -> bool:
        return True

    def __str__(self) -> str:
        return f'{" " * self.indent}{self.contents}'

@attr.s
class LabelStatement:
    context: Context = attr.ib()
    node: Node = attr.ib()

    def should_write(self) -> bool:
        return self.node in self.context.goto_nodes

    def __str__(self) -> str:
        return f'{label_for_node(self.node)}:'

Statement = Union[SimpleStatement, IfElseStatement, LabelStatement]

@attr.s
class Body:
    print_node_comment: bool = attr.ib()
    statements: List[Statement] = attr.ib(factory=list)

    def add_node(self, node: Node, indent: int, comment_empty: bool) -> None:
        assert isinstance(node.block.block_info, BlockInfo)
        to_write = node.block.block_info.to_write
        any_to_write = any(item.should_write() for item in to_write)

        # Add node header comment
        if self.print_node_comment and (any_to_write or comment_empty):
            self.add_comment(indent, f'Node {node.name()}')
        # Add node contents
        for item in node.block.block_info.to_write:
            if item.should_write():
                self.statements.append(SimpleStatement(indent, str(item)))

    def add_statement(self, statement: Statement) -> None:
        self.statements.append(statement)

    def add_comment(self, indent: int, contents: str) -> None:
        self.add_statement(SimpleStatement(indent, f'// {contents}'))

    def add_if_else(self, if_else: IfElseStatement) -> None:
        self.statements.append(if_else)

    def __str__(self) -> str:
        return '\n'.join(str(statement) for statement in self.statements
                if statement.should_write())


def label_for_node(node: Node) -> str:
    return f'block_{node.block.index}'

def emit_goto(context: Context, target: Node, body: Body, indent: int) -> None:
    label = label_for_node(target)
    context.goto_nodes.add(target)
    body.add_statement(SimpleStatement(indent, f'goto {label};'))


def build_conditional_subgraph(
    context: Context, start: ConditionalNode, end: Node, indent: int
) -> IfElseStatement:
    """
    Output the subgraph between "start" and "end" at indent level "indent",
    given that "start" is a ConditionalNode; this program will intelligently
    output if/else relationships.
    """
    if_block_info = start.block.block_info
    assert isinstance(if_block_info, BlockInfo)
    assert if_block_info.branch_condition is not None

    # If one of the output edges is the end, it's a "fake" if-statement. That
    # is, it actually just resides one indentation level above the start node.
    else_body = None
    if start.conditional_edge == end:
        assert start.fallthrough_edge != end  # otherwise two edges point to one node
        # If the conditional edge isn't real, then the "fallthrough_edge" is
        # actually within the inner if-statement. This means we have to negate
        # the fallthrough edge and go down that path.
        if_condition = if_block_info.branch_condition.negated()
        if_body = build_flowgraph_between(context, start.fallthrough_edge, end, indent + 4)
    elif start.fallthrough_edge == end:
        if_condition = if_block_info.branch_condition
        if not start.is_loop():
            # Only an if block, so this is easy.
            # I think this can only happen in the case where the other branch has
            # an early return.
            if_body = build_flowgraph_between(context, start.conditional_edge, end, indent + 4)
        else:
            # Don't want to follow the loop, otherwise we'd be trapped here.
            # Instead, write a goto for the beginning of the loop.
            if_body = Body(False, [])
            emit_goto(context, start.conditional_edge, if_body, indent + 4)
    else:
        # We need to see if this is a compound if-statement, i.e. containing
        # && or ||.
        conds = get_number_of_if_conditions(context, start, end)
        if conds < 2:  # normal if-statement
            # Both an if and an else block are present. We should write them in
            # chronological order (based on the original MIPS file). The
            # fallthrough edge will always be first, so write it that way.
            if_condition = if_block_info.branch_condition.negated()
            if_body = build_flowgraph_between(context, start.fallthrough_edge, end, indent + 4)
            else_body = build_flowgraph_between(context, start.conditional_edge, end, indent + 4)
        else:  # multiple conditions in if-statement
            return get_full_if_condition(context, conds, start, end, indent)

    return IfElseStatement(if_condition, indent, if_body=if_body, else_body=else_body)

def end_reachable_without(
    context: Context, start: Node, end: Node, without: Node
) -> bool:
    """Return whether "end" is reachable from "start" if "without" were removed.
    """
    if end == without or start == without:
        # Can't get to the end.
        return False
    if start == end:
        # Already there! (Base case.)
        return True

    key = (start, end, without)
    if key in context.reachable_without:
        return context.reachable_without[key]

    def reach(edge: Node) -> bool:
        return end_reachable_without(context, edge, end, without)

    if isinstance(start, BasicNode):
        ret = reach(start.successor)
    elif isinstance(start, ConditionalNode):
        # Going through the conditional node cannot help, since that is a
        # backwards arrow. There is no way to get to the end.
        ret = (reach(start.fallthrough_edge) or
            (not start.is_loop() and reach(start.conditional_edge)))
    else:
        assert isinstance(start, ReturnNode)
        ret = False

    context.reachable_without[key] = ret
    return ret

def get_reachable_nodes(start: Node) -> Set[Node]:
    reachable_nodes: Set[Node] = set()
    stack: List[Node] = [start]
    while stack:
        node = stack.pop()
        if node in reachable_nodes:
            continue
        reachable_nodes.add(node)
        if isinstance(node, BasicNode):
            stack.append(node.successor)
        elif isinstance(node, ConditionalNode):
            if not node.is_loop():
                stack.append(node.conditional_edge)
            stack.append(node.fallthrough_edge)
    return reachable_nodes

def immediate_postdominator(context: Context, start: Node, end: Node) -> Node:
    """
    Find the immediate postdominator of "start", where "end" is an exit node
    from the control flow graph.
    """
    # If the end is unreachable, we are computing immediate postdominators
    # of a subflow where every path ends in an early return. In this case we
    # need to replace our end node, or else every node will be treated as a
    # postdominator, and the earliest one might be within a conditional
    # expression. That in turn can result in nodes emitted multiple times.
    # (TODO: this is rather ad hoc, we should probably come up with a more
    # principled approach to early returns...)
    reachable_nodes = get_reachable_nodes(start)
    if end not in reachable_nodes:
        end = max(reachable_nodes, key=lambda n: n.block.index)

    stack: List[Node] = [start]
    postdominators: List[Node] = []
    while stack:
        # Get potential postdominator.
        node = stack.pop()
        if node.block.index > end.block.index:
            # Don't go beyond the end.
            continue
        # Add children of node.
        if isinstance(node, BasicNode):
            stack.append(node.successor)
        elif isinstance(node, ConditionalNode):
            if not node.is_loop():
                # If the node is a loop, then adding the conditional edge
                # here would cause this while loop to never end.
                stack.append(node.conditional_edge)
            stack.append(node.fallthrough_edge)
        # If removing the node means the end becomes unreachable,
        # the node is a postdominator.
        if node != start and not end_reachable_without(context, start, end, node):
            postdominators.append(node)
    assert postdominators  # at least "end" should be a postdominator
    # Get the earliest postdominator
    postdominators.sort(key=lambda node: node.block.index)
    return postdominators[0]


def count_non_postdominated_parents(
    context: Context, child: Node, curr_end: Node
) -> int:
    """
    Return the number of parents of "child" for whom "child" is NOT their
    immediate postdominator. This is useful for finding nodes that would be
    printed more than once under naive assumptions, i.e. if-conditions that
    contain multiple predicates in the form of && or ||.
    """
    count = 0
    for parent in child.parents:
        if immediate_postdominator(context, parent, curr_end) != child:
            count += 1
    # Ideally, either all this node's parents are immediately postdominated by
    # it, or none of them are. In practice this doesn't always hold, and then
    # output of && and || may not be correct.
    if count not in [0, len(child.parents)] and not context.has_warned:
        context.has_warned = True
        print("Warning: confusing control flow, output may have incorrect && "
            "and || detection. Run with --no-andor to disable detection and "
            "print gotos instead.\n")
    return count


def get_number_of_if_conditions(
    context: Context, node: ConditionalNode, curr_end: Node
) -> int:
    """
    For a given ConditionalNode, this function will return k when the if-
    statement of the correspondant C code is "if (1 && 2 && ... && k)" or
    "if (1 || 2 || ... || k)", where the numbers are labels for clauses.
    (It remains unclear how a predicate that mixes && and || would behave.)
    """
    if not context.options.andor_detection:
        # If &&/|| detection is disabled, short-circuit this logic and return
        # 1 instead.
        return 1

    count1 = count_non_postdominated_parents(context, node.conditional_edge,
                                             curr_end)
    count2 = count_non_postdominated_parents(context, node.fallthrough_edge,
                                             curr_end)

    # Return the nonzero count; the predicates will go through that path.
    # (TODO: I have a theory that we can just return count2 here.)
    if count1 != 0:
        return count1
    else:
        return count2

def join_conditions(
    conditions: List[Condition], op: str, only_negate_last: bool
) -> Condition:
    assert op in ['&&', '||']
    assert conditions
    final_cond: Optional[Condition] = None
    for i, cond in enumerate(conditions):
        if not only_negate_last or i == len(conditions) - 1:
            cond = cond.negated()
        if final_cond is None:
            final_cond = cond
        else:
            final_cond = BinaryOp(final_cond, op, cond, type=Type.bool())
    assert final_cond is not None
    return final_cond

def get_full_if_condition(
    context: Context,
    count: int,
    start: ConditionalNode,
    curr_end: Node,
    indent: int
) -> IfElseStatement:
    curr_node: Node = start
    prev_node: Optional[ConditionalNode] = None
    conditions: List[Condition] = []
    # Get every condition.
    while count > 0:
        if not isinstance(curr_node, ConditionalNode):
            raise DecompFailure("Complex control flow; node assumed to be "
                "part of &&/|| wasn't. Run with --no-andor to disable "
                "detection of &&/|| and try again.")
        block_info = curr_node.block.block_info
        assert isinstance(block_info, BlockInfo)
        assert block_info.branch_condition is not None
        conditions.append(block_info.branch_condition)
        prev_node = curr_node
        curr_node = curr_node.fallthrough_edge
        count -= 1
    # At the end, if we end up at the conditional-edge after the very start,
    # then we know this was an || statement - if the start condition were true,
    # we would have skipped ahead to the body.
    if curr_node == start.conditional_edge:
        assert prev_node is not None
        return IfElseStatement(
            # Negate the last condition, for it must fall-through to the
            # body instead of jumping to it, hence it must jump OVER the body.
            join_conditions(conditions, '||', only_negate_last=True),
            indent,
            if_body=build_flowgraph_between(
                context, start.conditional_edge, curr_end, indent + 4),
            # The else-body is wherever the code jumps to instead of the
            # fallthrough (i.e. if-body).
            else_body=build_flowgraph_between(
                context, prev_node.conditional_edge, curr_end, indent + 4)
        )
    # Otherwise, we have an && statement.
    else:
        return IfElseStatement(
            # We negate everything, because the conditional edges will jump
            # OVER the if body.
            join_conditions(conditions, '&&', only_negate_last=False),
            indent,
            if_body=build_flowgraph_between(
                context, curr_node, curr_end, indent + 4),
            else_body=build_flowgraph_between(
                context, start.conditional_edge, curr_end, indent + 4)
        )

def write_return(
    context: Context, body: Body, node: ReturnNode, indent: int, last: bool
) -> None:
    if last:
        body.add_statement(LabelStatement(context, node))
    body.add_node(node, indent, comment_empty=node.is_real())

    ret_info = node.block.block_info
    assert isinstance(ret_info, BlockInfo)

    ret = ret_info.return_value
    if ret is not None:
        ret_str = stringify_expr(as_type(ret, context.return_type, True))
        body.add_statement(SimpleStatement(indent, f'return {ret_str};'))
        context.is_void = False
    elif not last:
        body.add_statement(SimpleStatement(indent, 'return;'))


def build_flowgraph_between(
    context: Context, start: Node, end: Node, indent: int
) -> Body:
    """
    Output a section of a flow graph that has already been translated to our
    symbolic AST. All nodes between start and end, including start but NOT end,
    will be printed out using if-else statements and block info at the given
    level of indentation.
    """
    curr_start = start
    body = Body(print_node_comment=context.options.debug)

    # We will split this graph into subgraphs, where the entrance and exit nodes
    # of that subgraph are at the same indentation level. "curr_start" will
    # iterate through these nodes, which are commonly referred to as
    # articulation nodes.
    while curr_start != end:
        # Write the current node (but return nodes are handled specially).
        if not isinstance(curr_start, ReturnNode):
            # If a node is ever encountered twice, we can emit a goto to the
            # first place we emitted it. Since nodes represent positions in the
            # assembly, and we use phi's for preserved variable contents, this
            # will end up semantically equivalent. This can happen sometimes
            # when early returns/continues/|| are not detected correctly, and
            # hints at that situation better than if we just blindly duplicate
            # the block.
            if curr_start in context.emitted_nodes:
                emit_goto(context, curr_start, body, indent)
                break
            context.emitted_nodes.add(curr_start)

            # Emit a label for the node (which is only printed if something
            # jumps to it, e.g. currently for loops).
            body.add_statement(LabelStatement(context, curr_start))
            body.add_node(curr_start, indent, comment_empty=True)

        if isinstance(curr_start, BasicNode):
            # In a BasicNode, the successor is the next articulation node.
            curr_start = curr_start.successor
        elif isinstance(curr_start, ConditionalNode):
            # A ConditionalNode means we need to find the next articulation
            # node. This means we need to find the "immediate postdominator"
            # of the current node, where "postdominator" means we have to go
            # through it, and "immediate" means we aren't skipping any.
            curr_end = immediate_postdominator(context, curr_start, end)
            # We also need to handle the if-else block here; this does the
            # outputting of the subgraph between curr_start and the next
            # articulation node.
            body.add_if_else(
                build_conditional_subgraph(context, curr_start, curr_end, indent))
            # Move on.
            curr_start = curr_end
        else:
            assert isinstance(curr_start, ReturnNode)
            # Write the return node, and break, because there is nothing more
            # to process.
            write_return(context, body, curr_start, indent, last=False)
            break

    return body

def build_naive(context: Context, nodes: List[Node]) -> Body:
    """Naive procedure for generating output with only gotos for control flow.

    Used for --no-ifs, when the regular if_statements code fails."""

    body = Body(print_node_comment=context.options.debug)

    def emit_node(node: Node) -> None:
        body.add_statement(LabelStatement(context, node))
        body.add_node(node, 4, True)

    def maybe_emit_return(node: Node, sub_body: Body, indent: int) -> bool:
        if not isinstance(node, ReturnNode) or node.is_real():
            return False
        write_return(context, sub_body, node, indent, last=False)
        return True

    def emit_successor(node: Node, cur_index: int) -> None:
        if maybe_emit_return(node, body, 4):
            return
        if cur_index + 1 < len(nodes) and nodes[cur_index + 1] == node:
            # Fallthrough is fine
            return
        emit_goto(context, node, body, 4)

    for i, node in enumerate(nodes):
        if isinstance(node, ReturnNode):
            # Do not emit return nodes; they are often duplicated and don't
            # have a well-defined position, so we emit them next to where they
            # are jumped to instead.
            pass
        elif isinstance(node, BasicNode):
            emit_node(node)
            emit_successor(node.successor, i)
        else: # ConditionalNode
            emit_node(node)
            if_body = Body(print_node_comment=False)
            if not maybe_emit_return(node.conditional_edge, if_body, 8):
                emit_goto(context, node.conditional_edge, if_body, 8)
            block_info = node.block.block_info
            assert isinstance(block_info, BlockInfo)
            assert block_info.branch_condition is not None
            body.add_if_else(IfElseStatement(block_info.branch_condition, 4,
                if_body=if_body, else_body=None))
            emit_successor(node.fallthrough_edge, i)

    return body

def write_function(function_info: FunctionInfo, options: Options) -> None:
    context = Context(flow_graph=function_info.flow_graph, options=options)
    start_node: Node = context.flow_graph.entry_node()
    return_node: Optional[ReturnNode] = context.flow_graph.return_node()
    if return_node is None:
        fictive_block = Block(-1, None, None)
        return_node = ReturnNode(block=fictive_block, index=-1)

    if options.debug:
        print("Here's the whole function!\n")
    body: Body = (build_flowgraph_between(context, start_node, return_node, 4)
            if options.ifs else build_naive(context, context.flow_graph.nodes))

    if return_node.index != -1:
        write_return(context, body, return_node, 4, last=True)

    ret_type = 'void '
    if not context.is_void:
        ret_type = context.return_type.to_decl()
    fn_name = function_info.stack_info.function.name
    arg_strs = []
    for arg in function_info.stack_info.arguments:
        arg_strs.append(f'{arg.type.to_decl()}{arg}')
    arg_str = ', '.join(arg_strs) or 'void'
    print(f'{ret_type}{fn_name}({arg_str})\n{{')

    any_decl = False
    for local_var in function_info.stack_info.local_vars[::-1]:
        type_decl = local_var.type.to_decl()
        print(SimpleStatement(4, f'{type_decl}{local_var};'))
        any_decl = True
    temp_decls = set()
    for temp_var in function_info.stack_info.temp_vars:
        if temp_var.need_decl():
            expr = temp_var.expr
            type_decl = expr.type.to_decl()
            temp_decls.add(f'{type_decl}{expr.var};')
            any_decl = True
    for decl in sorted(list(temp_decls)):
        print(SimpleStatement(4, decl))
    for phi_var in function_info.stack_info.phi_vars:
        type_decl = phi_var.type.to_decl()
        print(SimpleStatement(4, f'{type_decl}{phi_var.get_var_name()};'))
        any_decl = True
    if any_decl:
        print()

    print(body)
    print('}')
