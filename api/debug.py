# -*- coding: utf-8 -*-
#
# gdb-frontend is a easy, flexible and extensionable gui debugger
#
# https://github.com/rohanrhu/gdb-frontend
# https://oguzhaneroglu.com/projects/gdb-frontend/
#
# Licensed under GNU/GPLv3
# Copyright (C) 2019, Oğuzhan Eroğlu (https://oguzhaneroglu.com/) <rohanrhu2@gmail.com>

"""
GDBFrontend Debugging API
All api.debug functions/methods are thread-safe.
They will be executed in GDB's main-thread and block caller thread.
"""

import os
import importlib
import threading
import traceback
import time
import sys

import config
import settings
import util
import api.flags
import api.globalvars

gdb = importlib.import_module("gdb")

def threadSafe(callback):
    """
    Decorator for running something with thread-safety.
    If it is currently on main-thread, callback runs immediately,
    otherwise it appends callback to gdb's event-loop
    and blocks caller thread until callback finish.
    
    Usage:\n
    @api.debug.threadSafe\n
    def threadSafeFunction():\n
        pass
    """

    def _threadSafe(*args, **kwargs):
        nonlocal callback

        is_mt = threading.current_thread() is threading.main_thread()
        lockCounter = util.AtomicInteger()

        output = None

        def _exec__mT():
            nonlocal callback
            nonlocal lockCounter
            nonlocal is_mt
            nonlocal output

            try:
                output = callback(*args, **kwargs)
            except Exception as e:
                print(traceback.format_exc())

            if not is_mt: lockCounter.decr()
            
        if not is_mt:
            lockCounter.incr()
            gdb.post_event(_exec__mT)
            while lockCounter.get() > 0: pass
        else:
            _exec__mT()

        return output

    return _threadSafe

def execCommand(command, buff_output=False):
    """
    Thread-safe GDB command execution.
    """

    is_mt = threading.current_thread() is threading.main_thread()
    lockCounter = util.AtomicInteger()

    output = None

    def _execCommand__mT():
        nonlocal lockCounter
        nonlocal is_mt
        nonlocal command
        nonlocal buff_output
        nonlocal output

        try:
            output = gdb.execute(command, to_string=buff_output)
        except Exception as e:
            print(traceback.format_exc())

        if not is_mt: lockCounter.decr()
        
    if not is_mt:
        lockCounter.incr()
        gdb.post_event(_execCommand__mT)
        while lockCounter.get() > 0: pass
    else:
        _execCommand__mT()

    return output

@threadSafe
def load(file):
    """
    Loads objfile by given path.
    Sets CWD to directory of objfile according to user-editable setting settings.SET_CWD_TO_EXECUTABLE.
    """

    try:
        gdb.execute("symbol-file")
        gdb.execute("file %s" % file)

        if settings.SET_CWD_TO_EXECUTABLE:
            gdb.execute("set cwd %s" % os.path.dirname(file))
        
        return True
    except Exception as e:
        util.verbose("[Error]", str(e))
        return False

@threadSafe
def connect(host, port):
    """
    Connects to gdbserver.
    """

    try:
        gdb.execute("target remote %s:%s" % (str(host), str(port)))
        return True
    except Exception as e:
        util.verbose("[Error]", str(e))
        return False

@threadSafe
def getState():
    """
    Returns all debugging information with JSON-serializability.
    """

    state = {}

    state["breakpoints"] = getBreakpoints()
    state["objfiles"] = getFiles()
    state["sources"] = getSources()

    try:
        current_frame = gdb.selected_frame()
        sal = current_frame.find_sal()
        symtab = sal.symtab
        
        state["current_location"] = {}
        state["current_location"]["file"] = symtab.fullname().replace("\\", "/")
        state["current_location"]["line"] = sal.line
    except Exception as e:
        state["current_location"] = False

    inferior = gdb.selected_inferior()
    
    try:
        threads = inferior.threads()
    except:
        threads = []

    state["inferior"] = {}
    state["inferior"]["num"] = inferior.num
    state["inferior"]["threads"] = []

    th0 = gdb.selected_thread()

    for _thread in threads:
        thread = {}
        thread["name"] = _thread.name
        thread["num"] = _thread.num
        thread["global_num"] = _thread.global_num
        thread["ptid"] = _thread.ptid
        thread["is_stopped"] = _thread.is_stopped()
        thread["is_running"] = _thread.is_running()
        thread["is_exited"] = _thread.is_exited()
        if th0 is None:
            thread["is_current"] = False
        else:
            thread["is_current"] = _thread.num == th0.num

        _thread.switch()

        if not _thread.is_running():
            frame = gdb.newest_frame()
            try:
                block = frame.block()
                function = block.function
            except RuntimeError as e:
                block = False
                function = False

            if function:
                file = function.symtab.filename
                line = function.line
                function_name = str(function.name)
            else:
                file = False
                line = False
                function_name = str(frame.name())

            backtrace = backTraceFrame(frame)

            backtrace_json = []
            for _frame in backtrace:
                _name = _frame.name()
                _function = _frame.function()

                _frame_json = {}
                _frame_json["pc"] = _frame.pc()
                _frame_json["function"] = _name
                _frame_json["file"] = {}
                if _function is not None:
                    _frame_json["line"] = _frame.find_sal().line
                    _frame_json["file"]["name"] = _function.symtab.filename
                    _frame_json["file"]["path"] = _function.symtab.fullname()
                else:
                    _frame_json["file"] = False

                backtrace_json.append(_frame_json)

            thread["frame"] = {}
            thread["frame"]["file"] = file
            thread["frame"]["line"] = line
            thread["frame"]["function"] = function_name
            thread["frame"]["backtrace"] = backtrace_json

        state["inferior"]["threads"].append(thread)

    if th0 is not None:
        th0.switch()

        if not th0.is_running():
            stack = backTraceFrame(gdb.newest_frame())
            selected_frames = api.globalvars.debugFlags.get(api.flags.AtomicDebugFlags.SELECTED_FRAMES)

            selected_frame = False

            for _ptid, _selected_frame in selected_frames.items():
                if _ptid == th0.ptid:
                    is_selected = False

                    for _frame in stack:
                        if _selected_frame == _frame.pc():
                            selected_frame = _frame
                            _frame.select()
                            is_selected = True
                            break

                    if not is_selected:
                        del selected_frames[_ptid]

                    break

            if not selected_frame:
                try:
                    selected_frame = gdb.selected_frame()
                except gdb.error:
                    selected_frame = False

            if selected_frame:
                try:
                    block = selected_frame.block()
                    function = block.function
                except RuntimeError as e:
                    block = False
                    function = False

                if function:
                    file = function.symtab.filename
                    path = function.symtab.fullname()
                    line = function.line
                    function_name = str(function.name)
                else:
                    file = False
                    path = False
                    line = False
                    function_name = str(selected_frame.name())

                backtrace = backTraceFrame(selected_frame)

                backtrace_json = []
                for _stack_frame in backtrace:
                    _name = _stack_frame.name()
                    _function = _stack_frame.function()

                    _stack_frame_json = {}
                    _stack_frame_json["pc"] = selected_frame.pc()
                    _stack_frame_json["function"] = _name
                    _stack_frame_json["file"] = {}
                    if _function is not None:
                        _stack_frame_json["line"] = _stack_frame.find_sal().line
                        _stack_frame_json["file"]["name"] = _function.symtab.filename
                        _stack_frame_json["file"]["path"] = _function.symtab.fullname()
                    else:
                        _stack_frame_json["file"] = False

                    backtrace_json.append(_stack_frame_json)

                variables = []

                try:
                    block = selected_frame.block()

                    while block:
                        for symbol in block:
                            if (symbol.is_argument or symbol.is_variable) and (symbol.name not in variables):
                                try:
                                    value = symbol.value(selected_frame)
                                except Exception as e:
                                    print("[Error]", e)

                                variable = {}
                                variable["is_global"] = block.is_global
                                variable["name"] = symbol.name
                                variable["is_pointer"] = symbol.type.code == gdb.TYPE_CODE_PTR

                                variable["value"] = ""

                                try:
                                    variable["value"] = value.string(length=1000)
                                    variable["is_nts"] = True
                                except gdb.error as e:
                                    variable["is_nts"] = False
                                    variable["value"] = str(value)
                                except UnicodeDecodeError as e:
                                    variable["is_nts"] = False
                                    variable["value"] = str(value)

                                if symbol.type:
                                    terminalType = resolveTerminalType(symbol.type)
                                    type_tree = serializableTypeTree(resolveTypeTree(symbol.type))

                                    variable["type"] = serializableType(symbol.type)
                                    variable["type"]["terminal"] = serializableType(terminalType)
                                    variable["type_tree"] = type_tree
                                else:
                                    variable["type"] = False

                                variables.append(variable)


                        block = block.superblock
                except Exception as e:
                    print(traceback.format_exc())

                state["selected_frame"] = {}
                state["selected_frame"]["pc"] = selected_frame.pc()
                if file:
                    state["selected_frame"]["file"] = {}
                    state["selected_frame"]["file"]["name"] = file
                    state["selected_frame"]["file"]["path"] = path
                else:
                    state["selected_frame"]["file"] = False
                state["selected_frame"]["line"] = line
                state["selected_frame"]["function"] = function_name
                state["selected_frame"]["backtrace"] = backtrace_json
                state["selected_frame"]["variables"] = variables
                state["selected_frame"]["disassembly"] = disassembleFrame()
            else:
                state["selected_frame"] = False
        else:
            state["selected_frame"] = False

    state["inferior"]["threads"].reverse()

    return state

@threadSafe
def getBreakpoints():
    """
    Returns JSON-serializable breakpoints list.
    """

    breakpoints = []

    for _breakpoint in gdb.breakpoints():
        _breakpoint_json = {}
        _breakpoint_json["number"] = _breakpoint.number
        _breakpoint_json["enabled"] = _breakpoint.enabled
        _breakpoint_json["location"] = _breakpoint.location
        _breakpoint_json["expression"] = _breakpoint.expression
        _breakpoint_json["condition"] = _breakpoint.condition
        _breakpoint_json["thread"] = _breakpoint.thread

        breakpoints.append(_breakpoint_json)

    return breakpoints

@threadSafe
def addBreakpoint(file, line):
    thread = gdb.selected_thread()

    if thread:
        is_running = gdb.selected_thread().is_running()
    else:
        is_running = False

    if is_running:
        api.globalvars.debugFlags.set(api.flags.AtomicDebugFlags.IS_INTERRUPTED_FOR_BREAKPOINT_ADD, {
            "file": file,
            "line": line,
        })

        gdb.execute("interrupt")
    else:
        bp = Breakpoint(
            source = file,
            line = line
        )

@threadSafe
def getBreakpoint(num):
    """
    Returns gdb.Breakpoint() object by gdb.Breakpoint().num.
    """

    num = int(num)

    for bp in gdb.breakpoints():
        if bp.number == num:
            return bp

    return False

@threadSafe
def delBreakpoint(bp):
    """
    Deletes GDBFrontend.Breakpoint object.
    """

    bp.delete()

@threadSafe
def getFiles():
    """
    Returns GDB.objfile objects in a list.
    """

    objfiles = []

    for _file in gdb.objfiles():
        objfiles.append({
            "name": _file.filename
        })

    return objfiles

@threadSafe
def getSources():
    """
    Returns all source files as serializable from GDB.
    """

    try:
        sources = gdb.execute("i sources", to_string=True).split("\n")
        if len(sources) < 3: return []
        return sources[2].replace("\\", "/").split(", ")
    except gdb.error as e:
        return []
    except Exception as e:
        util.verbose("[Error] An error occured:", e)
        return []

@threadSafe
def run():
    gdb.execute("r")

@threadSafe
def pause():
    gdb.execute("interrupt")

@threadSafe
def cont():
    gdb.execute("c")

@threadSafe
def stepOver():
    gdb.execute("n")

@threadSafe
def step():
    gdb.execute("s")

@threadSafe
def stepInstruction():
    gdb.execute("si")

@threadSafe
def switchThread(global_num):
    """
    Switches between threads by gdb.Thread().global_num.
    """

    global_num = int(global_num)

    inferior = gdb.selected_inferior()
    threads = inferior.threads()

    for _thread in threads:
        if _thread.global_num == global_num:
            _thread.switch()
            break

@threadSafe
def backTraceFrame(frame):
    """
    Returns stack frames upper from given frame in order of oldest to newest.
    """

    trace = []

    def _back(frame):
        parent = frame.older()

        if parent is not None:
            trace.append(parent)
            _back(parent)

    trace.append(frame)
    _back(frame)

    return trace

@threadSafe
def selectFrame(pc):
    """
    Select frame by given PC.
    PC registers for all frames maybe got from getState() once stop event.
    """

    is_switched = False

    thread = gdb.selected_thread()

    newest_frame = gdb.newest_frame()
    stack = backTraceFrame(newest_frame)

    for _frame in stack:
        if _frame.pc() == pc:
            _frame.select()

            frames = api.globalvars.debugFlags.get(api.flags.AtomicDebugFlags.SELECTED_FRAMES)
            frames[thread.ptid] = _frame.pc()

            is_switched = True
            break

    return is_switched

@threadSafe
def signal(posix_signal):
    """
    Sends given posix-signal to application.
    """

    try:
        gdb.execute("signal " + posix_signal)
    except Exception as e:
        print("[Error]", e)

@threadSafe
def terminate():
    """
    Terminates the application.
    """

    is_need_interrupt = False

    try:
        gdb.execute("kill")
    except Exception as e:
        print("[Error]", e)
        is_need_interrupt = True

    if is_need_interrupt and (gdb.selected_inferior().threads().__len__() > 0):
        try:
            api.globalvars.debugFlags.set(api.flags.AtomicDebugFlags.IS_INTERRUPTED_FOR_TERMINATE, True)
            gdb.execute("interrupt")
        except Exception as e:
            print("[Error]", e)
            api.globalvars.debugFlags.set(api.flags.AtomicDebugFlags.IS_INTERRUPTED_FOR_TERMINATE, False)

@threadSafe
def resolveTerminalType(ctype):
    """
    Returns terminal C-type of given type.
    """

    while True:
        try:
            ctype = ctype.target()
        except:
            break

    return ctype

@threadSafe
def resolveTypeTree(ctype):
    """
    Returns C-type tree of given type.
    """

    tree = [ctype]

    while True:
        try:
            ctype = ctype.target()
        except:
            break

        tree.append(ctype)

    return tree

@threadSafe
def resolveNonPointer(tree):
    """
    Returns first non-ptr C type on type tree.
    """

    for ctype in tree:
        if ctype.code != gdb.TYPE_CODE_PTR:
            return ctype

@threadSafe
def serializableTypeTree(tree):
    """
    Returns C-type tree of given type.
    """

    return [serializableType(ctype) for ctype in tree]

@threadSafe
def serializableType(ctype):
    """
    Returns serializable dict of C type.
    """

    serializable = {}
    serializable["alignof"] = ctype.alignof
    serializable["code"] = ctype.code
    serializable["name"] = ctype.name
    serializable["sizeof"] = ctype.sizeof
    serializable["tag"] = ctype.tag
    serializable["is_pointer"] = ctype.code == gdb.TYPE_CODE_PTR

    return serializable

@threadSafe
def serializableRepresentation(value):
    """
    Returns serializable value to string representation dict from gdb.Value.
    """

    serializable = {}

    try:
        serializable["value"] = value.string()
        serializable["is_nts"] = True
    except gdb.error as e:
        serializable["is_nts"] = False
        serializable["value"] = str(value)
    except UnicodeDecodeError as e:
        serializable["is_nts"] = False
        serializable["value"] = str(value)

    return serializable

@threadSafe
def getSerializableStructMembers(value, ctype):
    members = []

    if ctype.code not in [gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION]:
        return members

    try:
        if str(value) == "0x0":
            return None
    except gdb.error as e:
        return None

    for _field in ctype.fields():
        member = {}
        try:
            memberValue = value[_field.name]
        except gdb.error:
            continue

        if hasattr(_field, "bitpos"):
            member["bitpos"] = _field.bitpos

        if hasattr(_field, "enumval"):
            member["enumval"] = _field.enumval

        try:
            member["value"] = memberValue.string()
            member["is_nts"] = True
        except gdb.error as e:
            try:
                member["is_nts"] = False
                member["value"] = str(memberValue)
            except gdb.MemoryError as e:
                continue
        except UnicodeDecodeError as e:
            member["is_nts"] = False
            member["value"] = str(memberValue)

        member["name"] = _field.name
        member["is_pointer"] = _field.type.code == gdb.TYPE_CODE_PTR
        member["address"] = str(memberValue.address) if memberValue.address else "0x0"
        member["is_base_class"] = _field.is_base_class
        member["artificial"] = _field.artificial
        member["bitsize"] = _field.bitsize
        member["type"] = serializableType(_field.type)
        member["type"]["terminal"] = serializableType(resolveTerminalType(_field.type))
        member["type_tree"] = serializableTypeTree(resolveTypeTree(_field.type))
        member["parent_type"] = serializableTypeTree(resolveTypeTree(_field.type))

        members.append(member)

    return members

@threadSafe
def getVariable(name):
    """
    Returns C structure/union variable with members
    or pointers with what they point to.
    """

    frame = gdb.selected_frame()

    for symbol in frame.block():
        if symbol.name == name:
            return Variable(frame, symbol)

    return False

@threadSafe
def getVariableByExpression(tree, no_error=False):
    """
    Returns C member (api.debug.Variable) on current frame
    by given variable[->member](s) names tree.
    """

    try:
        value = gdb.parse_and_eval(tree)
        variable = Variable(
            frame=gdb.selected_frame(),
            symbol=False,
            value=value,
            tree=tree
        )
    except gdb.error as e:
        if not no_error:
            print(traceback.format_exc())

        return None
    return variable

@threadSafe
def disassemble(start, end):
    """
    Returns serializable instructions from start adress to end address.
    """

    return gdb.selected_frame().architecture().disassemble(start, end)

@threadSafe
def disassembleFrame():
    """
    Returns serializable instructions in selected frame.
    """

    frame = gdb.selected_frame()
    block = frame.block()

    return disassemble(block.start, block.end-1)

class Breakpoint(gdb.Breakpoint):
    @threadSafe
    def __init__(
        self,
        source = None,
        line = None
    ):
        gdb.Breakpoint.__init__(
            self,
            source = source,
            line = line
        )

    def stop(self):
        return True

class Variable():
    """
    GDBFrontend's serializable variable/member class.
    """
    
    def __init__(self, frame, symbol=False, value=False, tree=False):
        self.frame = frame
        self.symbol = symbol
        self.value = value
        self.tree = tree

        if self.tree:
            self.name = self.tree.split(".")[-1]
        else:
            self.name = self.symbol.name

    @threadSafe
    def serializable(self):
        """
        Returns given lib.types.Variable object as JSON-serializable dict.
        """

        frame = self.frame
        symbol = self.symbol
        if symbol:
            value = symbol.value(frame)
        else:
            value = self.value
        block = frame.block()

        serializable = {}
        serializable["is_global"] = block.is_global
        serializable["name"] = self.name
        serializable["is_pointer"] = value.type.code == gdb.TYPE_CODE_PTR
        serializable["address"] = str(value.address) if value.address else "0x0"

        try:
            serializable["value"] = value.string()
            serializable["is_nts"] = True
        except gdb.error as e:
            try:
                serializable["is_nts"] = False
                serializable["value"] = str(value)
            except gdb.MemoryError as e:
                return None
        except UnicodeDecodeError as e:
            serializable["is_nts"] = False
            serializable["value"] = str(value)

        if value.type:
            terminalType = resolveTerminalType(value.type)
            type_tree = resolveTypeTree(value.type)

            serializable["type"] = serializableType(value.type)
            serializable["type"]["terminal"] = serializableType(terminalType)
            serializable["type_tree"] = serializableTypeTree(type_tree)

            serializable["members"] = getSerializableStructMembers(value, terminalType)
        else:
            serializable["type"] = False

        return serializable