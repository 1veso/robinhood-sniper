"""Windows job ownership: close the handle to terminate all remaining child processes."""
import os

class ProcessTree:
    def __init__(self, process):
        self.process=process
        self.handle=None
        if os.name!='nt':return
        import ctypes
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_=[('per_process',ctypes.c_longlong),('per_job',ctypes.c_longlong),
                ('flags',w.DWORD),('min_working',ctypes.c_size_t),('max_working',ctypes.c_size_t),
                ('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(name,ctypes.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_=[('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),
                ('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];kernel.CreateJobObjectW.restype=w.HANDLE
        kernel.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        kernel.CloseHandle.argtypes=[w.HANDLE]
        handle=kernel.CreateJobObjectW(None,None)
        if not handle:raise ctypes.WinError(ctypes.get_last_error())
        info=Extended();info.basic.flags=0x2000 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not kernel.SetInformationJobObject(handle,9,ctypes.byref(info),ctypes.sizeof(info))
                or not kernel.AssignProcessToJobObject(handle,w.HANDLE(int(process._handle)))):
            error=ctypes.get_last_error();kernel.CloseHandle(handle)
            if process.poll() is None:process.kill()
            raise ctypes.WinError(error)
        self.kernel,self.handle=kernel,handle

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle);self.handle=None
        elif os.name!='nt' and self.process.poll() is None:
            import signal
            try:os.killpg(self.process.pid,signal.SIGKILL)
            except ProcessLookupError:pass

def launch(command, **kwargs):
    """Assign a Windows child while suspended, before a venv launcher can fork."""
    import subprocess
    if os.name=='nt':kwargs['creationflags']=kwargs.get('creationflags',0)|0x4 # CREATE_SUSPENDED
    process=subprocess.Popen(command,**kwargs)
    tree=None
    try:
        tree=ProcessTree(process)
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes as w
            class ThreadEntry(ctypes.Structure):
                _fields_=[('size',w.DWORD),('usage',w.DWORD),('thread',w.DWORD),('owner',w.DWORD),
                          ('base_priority',w.LONG),('delta_priority',w.LONG),('flags',w.DWORD)]
            k=tree.kernel
            k.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];k.CreateToolhelp32Snapshot.restype=w.HANDLE
            k.Thread32First.argtypes=[w.HANDLE,ctypes.POINTER(ThreadEntry)]
            k.Thread32Next.argtypes=[w.HANDLE,ctypes.POINTER(ThreadEntry)]
            k.OpenThread.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenThread.restype=w.HANDLE
            k.ResumeThread.argtypes=[w.HANDLE];k.ResumeThread.restype=w.DWORD
            snapshot=k.CreateToolhelp32Snapshot(4,0)
            if snapshot==w.HANDLE(-1).value:raise ctypes.WinError(ctypes.get_last_error())
            resumed=False
            try:
                entry=ThreadEntry();entry.size=ctypes.sizeof(entry)
                found=k.Thread32First(snapshot,ctypes.byref(entry))
                while found:
                    if entry.owner==process.pid:
                        handle=k.OpenThread(2,False,entry.thread)
                        if not handle:raise ctypes.WinError(ctypes.get_last_error())
                        try:
                            if k.ResumeThread(handle)==0xffffffff:raise ctypes.WinError(ctypes.get_last_error())
                            resumed=True
                        finally:k.CloseHandle(handle)
                        break
                    found=k.Thread32Next(snapshot,ctypes.byref(entry))
                if not resumed:raise OSError('Suspended worker primary thread not found')
            finally:k.CloseHandle(snapshot)
        return process,tree
    except BaseException:
        if tree:tree.close()
        elif process.poll() is None:process.kill()
        process.wait(timeout=5)
        raise
