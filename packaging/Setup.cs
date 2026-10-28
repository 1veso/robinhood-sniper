using System;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Diagnostics;
using System.Windows.Forms;
using Microsoft.Win32;

class Setup {
    const string PayloadHash="PAYLOAD_HASH";
    static string Hash(Stream s){using(var sha=SHA256.Create()){return BitConverter.ToString(sha.ComputeHash(s)).Replace("-","").ToLowerInvariant();}}
    static string Q(string s){return "\""+s.Replace("\"","\\\"").TrimEnd('\\')+"\"";}
    static string Run(string file,string args){using(var p=Process.Start(new ProcessStartInfo(file,args){UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true})){string s=p.StandardOutput.ReadToEnd();p.StandardError.ReadToEnd();p.WaitForExit();if(p.ExitCode!=0)throw new Exception("Setup command failed; existing accounts are preserved.");return s.Trim();}}
    static void Extract(string root){
        if(Directory.Exists(root)&&Directory.GetFileSystemEntries(root).Length>0)throw new Exception("Destination is not empty; no existing installation will be overwritten.");
        using(var s=Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip")){
            if(Hash(s)!=PayloadHash)throw new Exception("Embedded payload checksum mismatch");s.Position=0;
            using(var zip=new ZipArchive(s,ZipArchiveMode.Read)){
                string prefix=Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar)+Path.DirectorySeparatorChar;
                foreach(var entry in zip.Entries){
                    string target=Path.GetFullPath(Path.Combine(root,entry.FullName));
                    if(!target.StartsWith(prefix,StringComparison.OrdinalIgnoreCase)||entry.FullName.Contains(":"))throw new Exception("Invalid package path");
                    Directory.CreateDirectory(Path.GetDirectoryName(target));using(var input=entry.Open())using(var output=new FileStream(target,FileMode.CreateNew)){input.CopyTo(output);}
                }
            }
        }
        foreach(string line in File.ReadAllLines(Path.Combine(root,"payload.sha256"))){
            if(line.Length<67)throw new Exception("Invalid file manifest");
            string path=Path.GetFullPath(Path.Combine(root,line.Substring(66)));
            string prefix=Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar)+Path.DirectorySeparatorChar;
            if(!path.StartsWith(prefix,StringComparison.OrdinalIgnoreCase))throw new Exception("Invalid manifest path");
            using(var s=File.OpenRead(path)){if(Hash(s)!=line.Substring(0,64))throw new Exception("Installed file checksum mismatch");}
        }
    }
    static string Existing(){
        string script="$s=New-Object -ComObject 'Schedule.Service';$s.Connect();$f=$s.GetFolder('\\');try{$t=$f.GetTask('RUG DESK Console');$t.Definition.Actions.Item(1).WorkingDirectory}catch{if($_.Exception.HResult -notin @(-2147024894,-2147352567)){throw}}";
        return Run("powershell.exe","-NoProfile -NonInteractive -Command "+Q(script));
    }
    [STAThread]static int Main(string[] args){
        Application.EnableVisualStyles();
        try{
            // Isolated validation: extraction only, no registry/tasks/accounts/network.
            if(args.Length==2&&args[0]=="--extract-only"){Extract(Path.GetFullPath(args[1]));return 0;}
            if(!Environment.Is64BitOperatingSystem)throw new Exception("This package requires 64-bit Windows 10 or 11.");
            string destination=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","RugDesk");
            string existing=Existing();
            bool attach=args.Length==2&&args[0]=="--attach-existing";
            if(attach&&(existing.Length==0||!String.Equals(Path.GetFullPath(args[1]).TrimEnd('\\'),Path.GetFullPath(existing).TrimEnd('\\'),StringComparison.OrdinalIgnoreCase)))throw new Exception("Requested account does not match the existing console task. No attachment was made.");
            if(existing.Length>0&&!File.Exists(Path.Combine(existing,"desktop.py")))throw new Exception("An existing RUG DESK account was detected. Update its desktop launcher support before installing; no tasks or accounts were changed.");
            bool installed=File.Exists(Path.Combine(destination,"RugDesk.exe"));
            if(!installed){
                if(!attach&&MessageBox.Show("Install RUG DESK Paper for this Windows user?\n\n"+destination+"\n\n"+(existing.Length>0?"Your existing trial will be opened. Its accounts and jobs stay unchanged.":"A fresh setup will open. Add your own credentials, pair Telegram, then start paper trading. No paid AI or real-money execution is enabled."),"RUG DESK Setup",MessageBoxButtons.OKCancel)!=DialogResult.OK)return 0;
                Extract(destination);
            }
            string app=existing.Length>0?existing:Path.Combine(destination,"app");
            if(existing.Length==0)Run(Path.Combine(destination,"python/python.exe"),Q(Path.Combine(app,"desktop.py"))+" initialize");
            string exe=Path.Combine(destination,"RugDesk.exe");string launcherArgs="--app-root "+Q(app);
            string shortcut=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),"RUG DESK.lnk");
            string escapedExe=exe.Replace("'","''"), escapedArgs=launcherArgs.Replace("'","''");
            string script="$s=(New-Object -ComObject WScript.Shell).CreateShortcut('"+shortcut.Replace("'","''")+"');$s.TargetPath='"+escapedExe+"';$s.Arguments='"+escapedArgs+"';$s.WorkingDirectory='"+destination.Replace("'","''")+"';$s.Save()";
            Run("powershell.exe","-NoProfile -NonInteractive -Command "+Q(script));
            using(var key=Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\RugDeskPaper")){
                key.SetValue("DisplayName","RUG DESK Paper");key.SetValue("DisplayVersion","0.1.19");key.SetValue("Publisher","RUG DESK local build");key.SetValue("InstallLocation",destination);key.SetValue("UninstallString",Q(exe)+" "+launcherArgs+" --uninstall");key.SetValue("NoModify",1);key.SetValue("NoRepair",1);
            }
            Process.Start(new ProcessStartInfo(exe,launcherArgs){UseShellExecute=true});return 0;
        }catch(Exception e){
            if(args.Length==2&&args[0]=="--extract-only"){File.WriteAllText(args[1]+".error.txt",e.Message);return 2;}
            MessageBox.Show(e.Message,"RUG DESK Setup",MessageBoxButtons.OK,MessageBoxIcon.Error);return 2;
        }
    }
}
