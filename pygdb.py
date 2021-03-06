#!/usr/bin/python2 -u
# encoding:utf-8
import os
import socket

__author__ = 'ling'

from os import waitpid, WIFSTOPPED, WIFEXITED, WIFSIGNALED, WEXITSTATUS, WTERMSIG, WSTOPSIG, O_CREAT, O_TRUNC, O_RDONLY, O_WRONLY
import struct
import sys
from zio import *
from breakpoint import *
from hard_breakpoint import *
from linux_struct import *
from cpuinfo import *
from libc import *
from strace import *

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO
from utils import *

import logging
# 创建一个logger
logger = logging.getLogger('pygdb')
logger.setLevel(logging.DEBUG)

formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')


def add_console_logger(level=logging.DEBUG):
    ch = logging.StreamHandler()
    ch.setLevel(level)
    # 定义handler的输出格式
    ch.setFormatter(formatter)
    # 给logger添加handler
    logger.addHandler(ch)


def add_file_logger(file, level=logging.DEBUG):
    fh = logging.FileHandler(file)
    fh.setLevel(level)
    fh.setFormatter(formatter)
    logger.addHandler(fh)


#from ptrace.debugger.backtrace import getBacktrace

add_file_logger('pygdb.log')
# add_console_logger()

if not check_support():
    raise Exception('pygdb only support Linux os')

if sys.version[0] != "2":
    raise Exception('pygdb only support python2')


class pygdb:
    def __init__(self):
        self.pid = 0

        self.single_step_flag = False
        self.trace_sys_call_flag = False

        self.signal_handle_mode = {SIGTRAP: True}  # 字典，表示是否忽视该信号。如果忽视该信号，该信号将不会发送给被调试进程。

        self.callbacks = {}  # 字典，对应每个信号 有个对应的处理函数。 处理函数的返回值为返回给被调试程序的信号值。如果忽视该信号，返回0.

        self._restore_breakpoint = None  # 全局的一个标记，用于记录需要恢复的断点。
        # 断点的实现是将该地址改写为\xcc，如果需要恢复断点，那么需要将该地址改写为原来字节，
        # 同时单步运行，然后再次将该地址重写为\xcc，
        # 这里用_restore_braekpoint 临时记录下该断点。

        self.breakpoints = {}  # 所有的断点，以断点地址作为key值。
        self.event_handles = {}  # 对应事件的handler。
        # 共有6个事件，其中主要有EXEC和FORK事件。

        self.hardware_breakpoints = {}

        # 这里，还对event_hanles做了扩展，将单步断点等的处理函数也添加到event_handles中，但是在ptrace中并没有单步事件的说法。
        self.regs = None  # 寄存器，每次断下之后，都会读取寄存器放入到self.regs中。

        if CPU_64BITS:
            self.bit = 64
            self.byte = 8
        else:
            self.bit = 32
            self.byte = 4

        self.trace_fork = True
        self.trace_exec = True
        self.trace_clone = True

        self.pid_dict = {}

        self.process_exist = False

    #####################################################################################
    # some operation of start/close debug
    # load a pe file
    def load(self, target):
        logger.debug('target=%s' % target)
        args = split_command_line(target)
        pid = libc_fork()
        if pid == 0:  # child process
            self._ptrace(PTRACE_TRACEME, 0, 0, 0)
            infile = None
            outfile = None
            real_args = []
            for arg in args:
                if arg.startswith('<'):
                    infile = arg[1:]
                elif arg.startswith('>'):
                    outfile = arg[1:]
                else:
                    real_args.append(arg)
            if infile is not None:
                os.close(0)
                f = os.open(infile, O_RDONLY)
                os.dup2(f, 0)
            if outfile is not None:
                os.close(1)
                f = os.open(outfile, O_WRONLY | O_CREAT | O_TRUNC, 0666)
                os.dup2(f, 1)
            command = real_args[0]
            os.execv(command, real_args)
        else:  # parent
            # (pid, status) = waitpid(self.pid, 0)
            (pid, status) = waitpid(0, 0)
            self.pid = pid
            self.pid_dict[pid] = 0
            logger.debug('pid=%d' % pid)
            # return self.pid
            return pid

    # attach a running process
    def attach(self, pid):
        logger.debug('attach pid=%d' % pid)
        self._ptrace(PTRACE_ATTACH, pid, 0, 0)
        self.pid = pid

    # detach a debugged process
    def detach(self, signum=0):
        logger.debug('detached')
        self._ptrace(PTRACE_DETACH, self.pid, 0, signum)
        self.pid = 0

    # kill the debugged process
    def kill(self):
        logger.debug('killed')
        self._ptrace(PTRACE_KILL, self.pid, 0, 0)

    def killall(self):
        logger.debug('all killed')
        for key,value in self.pid_dict.items():
            self._ptrace(PTRACE_KILL, key, 0, 0)


    #################################################################################
    # about the stdout
    # redir the stdout to a network port
    def redir_stdout(self, ip, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((ip, port))

        fd = sock.makefile('w')
        sys.stdout = fd
        sys.stdin = fd
        sys.stderr = fd

    #################################################################################
    # about the memory and regs operation
    def read(self, address, length):
        logger.debug('read addr=%x length=%d' % (address, length))
        return self._read_process_memory(address, length)

    # todo
    def _read_process_memory(self, address, length):
        # self._log('read_process_memory addr=%x length=%d' % (address, length))
        data = ''

        byte = self.byte
        for i in range((length + byte - 1) / byte):
            value = self._ptrace(PTRACE_PEEKDATA, self.pid, address + i * byte, 0)
            # self._log('peek:addr=%x value=%x' % (address + i * byte, value))
            if byte == 8:
                data += l64(value)
            else:
                data += l32(value)
        data = data[0:length]
        return data

    def write(self, address, data, length=0):
        logger.debug('write address=%x data=' % address + repr(data))
        self._write_process_memory(address, data, length)

    # todo
    def _write_process_memory(self, address, data, length=0):
        if length == 0:
            length = len(data)

        if length == 0:
            return

        tmp_data = data
        byte = self.byte
        if length % byte:
            tmp_data += self._read_process_memory(address + length, byte - length % byte)

        for i in range(len(tmp_data) / byte):
            if byte == 4:
                self._ptrace(PTRACE_POKEDATA, self.pid, address + i * byte, l32(tmp_data[i * byte:i * byte + byte]))
            else:
                self._ptrace(PTRACE_POKEDATA, self.pid, address + i * byte, l64(tmp_data[i * byte:i * byte + byte]))

    def get_regs(self):
        regs = user_regs_struct()
        self._ptrace(PTRACE_GETREGS, self.pid, 0, addressof(regs))
        return regs

    def set_regs(self, regs):
        self._ptrace(PTRACE_SETREGS, self.pid, 0, addressof(regs))

    def get_debug_regs(self, index):
        dr = c_ulong()
        if CPU_64BITS:
            dr = self._ptrace(PTRACE_PEEKUSER, self.pid, 848 + index * 8, 0)
        else:
            dr = self._ptrace(PTRACE_PEEKUSER, self.pid, 252 + index * 4, 0)

        return dr

    def set_debug_regs(self, index, value):
        #print 'index=%d, value=%08x' %(index, value)
        if CPU_64BITS:
            self._ptrace(PTRACE_POKEUSER, self.pid, 848 + index * 8, value)
        else:
            self._ptrace(PTRACE_POKEUSER, self.pid, 252 + index * 4, value)

    ########################################################################
    # about the /proc operation
    # todo
    def print_vmmap(self):
        maps_file_path = '/proc/' + str(self.pid) + '/maps'
        f = open(maps_file_path, 'rb')
        d = f.read()
        f.close()

    ##############################################################################
    # about the breakpoint
    def bp_del(self, address):
        logger.debug('bp_del address=%x' % address)
        if (address in self.breakpoints.keys()) & (self.breakpoints[address] is not None):
            self.write(address, self.breakpoints[address].original_byte)
            self.breakpoints[address] = None

    def bp_del_all(self):
        logger.debug('bp_del_all')
        for key in self.breakpoints.keys():
            bp = self.breakpoints[key]
            if bp is not None:
                self.bp_del(bp)

        self.breakpoints = {}

    def bp_set(self, address, description="", restore=True, handler=None):
        logger.debug('bp_set address=%x' % address)
        original_byte = self.read(address, 1)
        self.write(address, '\xcc')
        self.breakpoints[address] = breakpoint(address, original_byte, description, restore, handler)

    def _del_hw_by_index(self, index):
        if not self.hardware_breakpoints.has_key(index):
            return

        self.set_debug_regs(index, 0)

        dr7 = self.get_debug_regs(7)

        if CPU_64BITS:
            align = (~((1 << (2 * index)) + (0xf << (16 + index * 4)))) & 0xffffffffffffffff
        else:
            align = (~((1 << (2 * index)) + (0xf << (16 + index * 4)))) & 0xffffffff

        dr7 &= align

        self.set_debug_regs(7, dr7)

        self.hardware_breakpoints.pop(index)

    def bp_del_hw(self, address):
        logger.debug("bp_del_hw(%08x)" % address)
        for i in range(4):
            if not self.hardware_breakpoints.has_key(i):
                continue

            if self.hardware_breakpoints[i].address == address:
                self._del_hw_by_index(i)

    def bp_set_hw(self, address, length, condition, restore=True, handler=None):
        logger.debug("bp_set_hw(%08x, %d, %s)" % (address, length, condition))

        if length not in [1, 2, 4]:
            logger.warn('invalid hw breakpoint length:%d' % length)
            return

        length -= 1

        if condition not in [HW_ACCESS, HW_EXECUTE, HW_WRITE]:
            logger.warn('invalid hw breakpoint condition:%d' % condition)
            return

        if not self.hardware_breakpoints.has_key(0):
            available = 0
        elif not self.hardware_breakpoints.has_key(1):
            available = 1
        elif not self.hardware_breakpoints.has_key(2):
            available = 2
        elif not self.hardware_breakpoints.has_key(3):
            available = 3
        else:
            logger.warn('not hard breakpoint slots avaliable')
            return

        # instantiate a new hardware breakpoint object for the new bp to create.
        hw_bp = hardware_breakpoint(address, length, condition, "", restore, handler=handler)

        dr7 = self.get_debug_regs(7)

        # mark available debug register as active (L0 - L3).
        # set the condition (RW0 - RW3) field for the appropriate slot (bits 16/17, 20/21, 24,25, 28/29)
        # set the length (LEN0-LEN3) field for the appropriate slot (bits 18/19, 22/23, 26/27, 30/31)
        self.set_debug_regs(7, dr7 | (1 << (available * 2)) | (condition << ((available * 4) + 16)) | (
            length << ((available * 4) + 18)))

        # set dr7 will clear dr0-dr3, why???

        hw_bp.slot = available
        self.hardware_breakpoints[available] = hw_bp

        '''
        # save our breakpoint address to the available hw bp slot.
        if available == 0:
            self.set_debug_regs(0, address)
        elif available == 1:
            self.set_debug_regs(1, address)
        elif available == 2:
            self.set_debug_regs(2, address)
        elif available == 3:
            self.set_debug_regs(3, address)
        '''

        for key in self.hardware_breakpoints.keys():
            self.set_debug_regs(key, self.hardware_breakpoints[key].address)


    #################################################################################
    def set_signal_handle_mode(self, signum, ignore=True):
        self.signal_handle_mode[signum] = ignore

    def set_options(self, pid, options):
        self._ptrace(PTRACE_SETOPTIONS, pid, 0, options)

    def set_callback(self, signum, callback_func=None):
        self.callbacks[signum] = callback_func

    def set_event_handle(self, event_code, handler=None):
        self.event_handles[event_code] = handler

    def single_step(self, enable):
        self.single_step_flag = enable

    def trace_sys_call(self, enable):
        self.trace_sys_call_flag = enable

    '''
    # True: trace child
    def follow_fork(self, mode):
        self.trace_fork = mode

    # True: trace parent
    def follow_exec(self, mode):
        self.trace_exec = mode

    def follow_clone(self, mode):
        self.trace_clone = mode
    '''

    ##########################################################################
    # about the debug event loop

    '''
        run(self):
        _debug_event_loop(self):
            _debug_event_iteration()
                #only exit
                _event_handle_process_exit(status)
                #only exit
                _event_handle_process_kill(status)
                #only exit
                _event_handle_process_unknown_status(status)
                #ptrace event
                _event_handle_process_ptrace_event(status)
                #signal event
                _event_handle_process_signal(status)
                    _event_handle_sigtrap()
                        _event_handle_single_step()
                        _event_handle_breakpoint()
                            self.breakpoints[bp_addr].handler
                    self.callbacks[signum](self)
    '''

    def run(self, options=None):
        if options is None:
            options = PTRACE_O_TRACESYSGOOD | PTRACE_O_TRACEFORK | PTRACE_O_TRACEVFORK | \
                      PTRACE_O_TRACEEXEC | PTRACE_O_TRACEVFORKDONE | PTRACE_O_TRACEEXIT

        self.set_options(self.pid, options)
        self._debug_event_loop()

    def _debug_event_loop(self):
        # continue
        if self.single_step_flag | (self._restore_breakpoint is not None):
            self._ptrace(PTRACE_SINGLESTEP, self.pid, 0, 0)
        elif self.trace_sys_call_flag:
            self._ptrace(PTRACE_SYSCALL, self.pid, 0, 0)
        else:
            self._ptrace(PTRACE_CONT, self.pid, 0, 0)

        while True:
            self._debug_event_iteration()
            if self.process_exist:
                break

    def _event_handle_process_exit(self, status, pid):
        code = WEXITSTATUS(status)
        logger.debug('process exited with code:%d, pid=%d' % (code, pid))
        self.pid_dict.pop(pid)
        if len(self.pid_dict) == 0:
            self.process_exist = True

    def _event_handle_process_kill(self, status, pid):
        signum = WTERMSIG(status)
        logger.debug('process killed by a signal:%d, pid=%d' % (signum, pid))
        self.pid_dict.pop(pid)
        if len(self.pid_dict) == 0:
            self.process_exist = True

    def _event_handle_process_unknown_status(self, status, pid):
        logger.debug('unknown process status:' + hex(status) + ', pid=%d' % pid)
        self.pid_dict.pop(pid)
        if len(self.pid_dict) == 0:
            self.process_exist = True

    def _event_handle_process_ptrace_event(self, status, pid):
        event = self.WPTRACEEVENT(status)
        logger.debug('ptrace event:%d-%s, pid=%d' % (event, event_name(event), pid))

        if event in self.event_handles.keys():
            self.event_handles[event](self)

        if event == PTRACE_EVENT_FORK:
            new_pid = self.get_eventmsg(pid)
            self.pid_dict[new_pid] = 0
            logger.debug('fork a child child:%d' % new_pid)
        elif event == PTRACE_EVENT_VFORK:
            logger.info('vfork event not support')
        elif event == PTRACE_EVENT_CLONE:
            new_pid = self.get_eventmsg(pid)
            self.pid_dict[new_pid] = 0
            logger.debug('clone a child child:%d' % new_pid)
        elif event == PTRACE_EVENT_EXEC:
            logger.info('exec event not support')
        elif event == PTRACE_EVENT_VFORK_DONE:
            logger.info('vfork event not support')
        elif event == PTRACE_EVENT_EXIT:
            return False
        return True

    def _event_handle_breakpoint(self):
        signum = 0

        if CPU_64BITS:
            bp_addr = self.regs.rip - 1
        else:
            bp_addr = self.regs.eip - 1

        self.write(bp_addr, self.breakpoints[bp_addr].original_byte, 1)

        if CPU_64BITS:
            self.regs.rip = bp_addr
        else:
            self.regs.eip = bp_addr

        self.set_regs(self.regs)

        logger.debug('handle breakpoint:%08x' % bp_addr)
        if (bp_addr in self.breakpoints.keys()) & (self.breakpoints[bp_addr].handler is not None):
            signum = self.breakpoints[bp_addr].handler(self)

        if self.breakpoints[bp_addr].restore:
            self._restore_breakpoint = self.breakpoints[bp_addr]
        else:
            self.breakpoints[bp_addr] = None

        if signum is None:
            signum = 0
        return signum

    def _event_handle_sys_call(self, pid):
        if CPU_64BITS:
            cur_pc = self.regs.rip - 2
            sys_name = SYSCALL_NAMES[self.regs.orig_rax]
            arg0 = self.regs.rdi
            arg1 = self.regs.rsi
            arg2 = self.regs.rdx
        else:
            cur_pc = self.regs.eip - 2
            sys_name = SYSCALL_NAMES[self.regs.orig_eax]
            arg0 = self.regs.ebx
            arg1 = self.regs.ecx
            arg2 = self.regs.edx

        if sys_call_handlers.has_key(sys_name):
            sys_call_handlers[sys_name](self, cur_pc, sys_name, arg0, arg1, arg2, self.pid_dict[pid])
        else:
            default_sys_call_handler(self, cur_pc, sys_name, arg0, arg1, arg2, self.pid_dict[pid])
        if self.pid_dict[pid] == 1:
            self.pid_dict[pid] = 0
        else:
            self.pid_dict[pid] = 1
        return 0

    def _event_handle_single_step(self):
        logger.debug('handle single step')
        if self._restore_breakpoint is not None:
            # restore breakpoint
            logger.debug('restore breakpoint')
            bp = self._restore_breakpoint
            self.bp_set(bp.address, bp.description, bp.restore, bp.handler)
            self._restore_breakpoint = None

        elif (PTRACE_EVENT_SINGLE_STEP in self.event_handles.keys()): 
            logger.debug("event_handles has PTRACE_EVENT_SINGLE_STEP")
            if self.event_handles[PTRACE_EVENT_SINGLE_STEP] is not None:
                logger.debug('call single callback')
                self.event_handles[PTRACE_EVENT_SINGLE_STEP](self)
        else:
            logger.debug('a single step error in process')
            return SIGTRAP #单步异常是由程序自己产生的
        return 0

    def _event_handle_sigtrap(self, pid, is_sys_call=False):
        logger.debug('handle sigtrap')
        self.regs = self.get_regs()

        if is_sys_call:
            return self._event_handle_sys_call(pid)

        dr6 = self.get_debug_regs(6)
        logger.debug('dr6=%08x' % dr6)

        # if self.single_step_flag:
        if (dr6 & 0x4000) == 0x4000:  # check the bs bit
            signum = self._event_handle_single_step()
            self.set_debug_regs(6, 0)
            return signum

        self.hardware_breakpoint_hit = None
        if (dr6 & 0x1) and self.hardware_breakpoints.has_key(0):
            self.hardware_breakpoint_hit = self.hardware_breakpoints[0]

        elif (dr6 & 0x2) and self.hardware_breakpoints.has_key(1):
            self.hardware_breakpoint_hit = self.hardware_breakpoints[1]

        elif (dr6 & 0x4) and self.hardware_breakpoints.has_key(2):
            self.hardware_breakpoint_hit = self.hardware_breakpoints[2]

        elif (dr6 & 0x8) and self.hardware_breakpoints.has_key(3):
            self.hardware_breakpoint_hit = self.hardware_breakpoints[3]

        # if we are dealing with a hardware breakpoint and there is a specific handler registered, pass control to it.
        if self.hardware_breakpoint_hit and self.hardware_breakpoint_hit.handler:
            signum = self.hardware_breakpoint_hit.handler(self)

            if not self.hardware_breakpoint_hit.restore:
                self.bp_del_hw(self.hardware_breakpoint_hit.address)

            self.set_debug_regs(6, 0)

            return signum

        if CPU_64BITS:
            if (self.regs.rip - 1) in self.breakpoints.keys():
                return self._event_handle_breakpoint()
        else:
            if (self.regs.eip - 1) in self.breakpoints.keys():
                return self._event_handle_breakpoint()
        return 0

    def generate_gcore(self, pid):
        os.popen('gcore -o core.' + str(pid) + ' ' + str(pid))

    def _event_handle_process_signal(self, status, pid):
        signum = WSTOPSIG(status)
        logger.debug('signum:%d-%s, pid=%d' % (signum & 0x7f, signal_name(signum & 0x7f), pid))
        dr6 = self.get_debug_regs(6)
        logger.debug('dr6=' + hex(dr6))
        dr7 = self.get_debug_regs(7)
        logger.debug('dr7=' + hex(dr7))

        self.regs = self.get_regs()

        dr6 = self.get_debug_regs(6)
        dr7 = self.get_debug_regs(7)

        if CPU_64BITS:
            logger.debug('rip=%08x' % self.regs.rip)
        else:
            logger.debug('eip=%08x' % self.regs.eip)

        if signum == SIGTRAP:
            if (0x80 | signum) == 0x80:
                is_sys_call = True
            else:
                is_sys_call = False
            return self._event_handle_sigtrap(pid, is_sys_call)

        if signum in self.callbacks.keys():
            return self.callbacks[signum](self)

        # ret
        if self.signal_handle_mode.has_key(signum):
            ignore = self.signal_handle_mode[signum]
            if ignore:
                if signum == SIGSEGV:
                    # self.print_vmmap()
                    self.generate_gcore(pid)
                return 0
        return signum

    def _debug_event_iteration(self):
        # (pid, status) = waitpid(self.pid, 0)
        (pid, status) = waitpid(0, 0)
        self.pid = pid
        signum = 0
        logger.debug('status:' + hex(status) + ' pid:' + str(pid))
        # Process exited?
        if WIFEXITED(status):
            self._event_handle_process_exit(status, pid)

        # Process killed by a signal?
        elif WIFSIGNALED(status):
            self._event_handle_process_kill(status, pid)

        # Invalid process status?
        elif not WIFSTOPPED(status):
            self._event_handle_process_unknown_status(status, pid)

        # Ptrace event?
        elif self.WPTRACEEVENT(status):
            self._event_handle_process_ptrace_event(status, pid)
        else:
            signum = self._event_handle_process_signal(status, pid)

        # continue
        if signum is None:
            signum = 0
        if self.single_step_flag | (self._restore_breakpoint is not None):
            self._ptrace(PTRACE_SINGLESTEP, pid, 0, signum)
        elif self.trace_sys_call_flag:
            self._ptrace(PTRACE_SYSCALL, pid, 0, signum)
        else:
            self._ptrace(PTRACE_CONT, pid, 0, signum)

    def WPTRACEEVENT(self, status):
        return status >> 16

    #################################################################
    def _ptrace(self, command, pid, arg1, arg2):
        logger.debug('ptrace command=%s' % ptrace_cmd_name(command))
        peek_commands = [PTRACE_PEEKDATA, PTRACE_PEEKSIGINFO, PTRACE_PEEKTEXT, PTRACE_PEEKUSER]
        if command in peek_commands:
            data = libc_ptrace(command, pid, arg1, arg2)
            # need to handle the error
            # to do
            return data

        if libc_ptrace(command, pid, arg1, arg2) == -1:
            logger.debug('ptrace error2:%d' % command)
            # libc_perror()

    def get_eventmsg(self, pid):
        new_pid = pid_t()
        self._ptrace(PTRACE_GETEVENTMSG, pid, 0, addressof(new_pid))
        return new_pid.value

    '''
    def getBacktrace(self, max_args=6, max_depth=20):
        pass
    '''
    '''

    def bp_del_hw(self, address):
        pass

    def bp_del_hw_all(self):
        pass

    def bp_del_mem_all(self):
        pass

    def bp_is_ours(self, address_to_check):
        pass

    def bp_is_ours_mem(self, address_to_check):
        pass

    def bp_set_hw(self, address, length, condition, restore=True, handler=None):
        pass

    def dbg_print_all_debug_registers(self):
        pass

    def dbg_print_all_guarded_pages(self):
        pass

    def exception_handler_guard_page(self):
        pass
    '''

    '''
    def func_resolve(self, dll, function):
        pass
    '''

    '''
    def get_attr(self, attribute):
        if not hasattr(self, attribute):
            return None

        return getattr(self, attribute)

    def hide_debugger(self):
        pass

    def set_attr(self, attribute, value):
        if hasattr(self, attribute):
            setattr(self, attribute, value)

    def get_siginfo(self):
        info = siginfo()
        self.ptrace(PTRACE_GETSIGINFO, self.pid, 0, addressof(info))
        return info

    def set_siginfo(self, info):
        self.ptrace(PTRACE_SETSIGINFO, self.pid, 0, addressof(info))
    '''

    '''
    def ptrace_getfpregs(pid):
        fpregs = user_fpregs_struct()
        ptrace(PTRACE_GETFPREGS, pid, 0, addressof(fpregs))
        return fpregs

    def ptrace_setfpregs(self, pid, fpregs):
        ptrace(PTRACE_SETFPREGS, pid, 0, addressof(fpregs))

    def ptrace_getfpxregs(self, pid):
        fpxregs = user_fpxregs_struct()
        self.ptrace(PTRACE_GETFPXREGS, pid, 0, addressof(fpxregs))
        return fpxregs

    def ptrace_setfpxregs(self, pid, fpxregs):
        self.ptrace(PTRACE_SETFPXREGS, pid, 0, addressof(fpxregs))

'''
