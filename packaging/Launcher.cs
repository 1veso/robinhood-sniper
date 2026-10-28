using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading.Tasks;
using System.Windows.Forms;

class RugDesk : Form {
    string app, python;
    TextBox log = new TextBox();
    TextBox username = new TextBox();
    ComboBox input = new ComboBox();
    FlowLayoutPanel actions = new FlowLayoutPanel();
    public RugDesk(string root) {
        app = root;
        python = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "python", "python.exe");
        Text = "RUG DESK | Paper trading control"; Size = new Size(900, 680);
        MinimumSize = new Size(740, 590); StartPosition = FormStartPosition.CenterScreen;
        Font = new Font("Segoe UI", 10); BackColor = Color.FromArgb(20, 25, 32); ForeColor = Color.WhiteSmoke;
        var title = new Label {Text="RUG DESK  /  PAPER", Font=new Font("Segoe UI",22,FontStyle.Bold), Dock=DockStyle.Top, Height=60, Padding=new Padding(20,12,0,0)};
        var help = new Label {Dock=DockStyle.Top, Height=112, Padding=new Padding(20,8,20,0), Text=
            "Live market data. Simulated cash and fills. No mainnet paper-money deposits.\n"+
            "New PC: import your Telegram bot token, send SNIPER SETUP to your bot, then pair below.\n"+
            "Start paper enables background jobs and opens the browser console. Keep this PC awake and signed in.\n"+
            "Solana entries still need the approved Grok/X gate. Paid AI and real-money execution remain disabled."};
        actions.Dock=DockStyle.Top; actions.Height=98; actions.Padding=new Padding(16,5,10,0);
        Button("Start / continue paper", async delegate {await Run("start-paper"); await OpenConsole();});
        Button("Open console", async delegate {await OpenConsole();});
        Button("Refresh status", async delegate {await Run("status");});
        Button("Open local data", delegate {Directory.CreateDirectory(Path.Combine(app,"runtime")); Process.Start("explorer.exe", Q(Path.Combine(app,"runtime")));return Task.FromResult(0);});
        Button("Setup guide", delegate {Process.Start(new ProcessStartInfo(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"QUICK_START.txt")){UseShellExecute=true});return Task.FromResult(0);});
        var setup=new FlowLayoutPanel {Dock=DockStyle.Top, Height=88, Padding=new Padding(20,3,10,0)};
        input.DropDownStyle=ComboBoxStyle.DropDownList;input.Width=255;input.ForeColor=Color.Black;
        input.Items.AddRange(new object[]{"telegram-bot-token.txt","jupiter-server-api-key.txt","solana-trading-address.txt","evm-trading-address.txt","observation-rpc-url.txt","quote-rpc-url.txt","solana-rpc-url.txt"});input.SelectedIndex=0;
        var import = new Button {Text="Import local file", Width=150,Height=30,ForeColor=Color.Black};
        import.Click += async delegate {using(var dialog=new OpenFileDialog()){dialog.Title="Select the API credential or PUBLIC address file";if(dialog.ShowDialog()==DialogResult.OK) await Guard(async delegate {await Run("import-input "+Q(input.Text)+" "+Q(dialog.FileName));});}};
        username.Width=180;username.Text="your_bot_username";username.ForeColor=Color.Black;
        var pair=new Button {Text="Pair Telegram + test",Width=190,Height=30,ForeColor=Color.Black};
        pair.Click += async delegate {await Guard(async delegate {await Run("pair "+Q(username.Text.Trim()));});};
        setup.Controls.Add(input);setup.Controls.Add(import);setup.SetFlowBreak(import,true);setup.Controls.Add(username);setup.Controls.Add(pair);
        log.Multiline=true;log.ReadOnly=true;log.ScrollBars=ScrollBars.Both;log.Dock=DockStyle.Fill;
        log.BackColor=Color.FromArgb(28,34,43);log.ForeColor=Color.FromArgb(195,230,216);log.Font=new Font("Consolas",10);log.BorderStyle=BorderStyle.FixedSingle;
        var path=new Label {Dock=DockStyle.Bottom,Height=42,Padding=new Padding(20,5,10,0),Text="Account files: "+app+"\nClosing this window leaves scheduled workers running."};
        Controls.Add(log);Controls.Add(setup);Controls.Add(actions);Controls.Add(help);Controls.Add(title);Controls.Add(path);
        Shown += async delegate {await Guard(async delegate {await Run("status");});};
    }
    void Button(string name,Func<Task> call){var b=new Button {Text=name,Width=165,Height=34,ForeColor=Color.Black,BackColor=Color.Gainsboro};b.Click+=async delegate{await Guard(call);};actions.Controls.Add(b);}
    async Task Guard(Func<Task> call){actions.Enabled=false;try{await call();}catch(Exception e){log.Text=e.Message;}finally{actions.Enabled=true;}}
    async Task Run(string command){
        log.Text="Working...";
        var result=await Task.Run(()=>{
            var info=new ProcessStartInfo(python,Q(Path.Combine(app,"desktop.py"))+" "+command){UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true,WorkingDirectory=app};
            using(var p=Process.Start(info)){var output=p.StandardOutput.ReadToEnd();var errors=p.StandardError.ReadToEnd();p.WaitForExit();if(p.ExitCode!=0)throw new Exception(output.Length>0?output:"Setup failed. Inspect local worker status; no live execution enabled.");return output;}
        });
        try {var serializer=new System.Web.Script.Serialization.JavaScriptSerializer();var obj=serializer.DeserializeObject(result);log.Text=Format(obj,0);} catch{log.Text=result;}
    }
    static string Format(object value,int depth){
        var map=value as System.Collections.Generic.Dictionary<string,object>;
        if(map==null)return value==null?"unknown":value.ToString();
        var s="";foreach(var pair in map){s+=new string(' ',depth*2)+pair.Key.Replace('_',' ')+": ";if(pair.Value is System.Collections.Generic.Dictionary<string,object>)s+="\r\n"+Format(pair.Value,depth+1);else s+=Format(pair.Value,0)+"\r\n";}return s;
    }
    async Task OpenConsole(){
        // Starting this task is idempotent (IgnoreNew); it never restarts workers.
        bool ready=await Task.Run(()=>{
            var info=new ProcessStartInfo("schtasks.exe","/Run /TN \"RUG DESK Console\""){UseShellExecute=false,CreateNoWindow=true};using(var p=Process.Start(info)){p.WaitForExit(5000);}
            for(int i=0;i<20;i++){
                try{var req=System.Net.WebRequest.Create("http://127.0.0.1:8765/api/health");req.Timeout=1500;using(var response=req.GetResponse())using(var reader=new StreamReader(response.GetResponseStream())){if(reader.ReadToEnd().Contains("read_only"))return true;}}catch{}
                System.Threading.Thread.Sleep(500);
            }return false;
        });
        if(!ready)throw new Exception("Console is not ready. On a new PC, finish Telegram pairing and click Start / continue paper. Existing workers may still be starting; refresh status.");
        Process.Start(new ProcessStartInfo("http://127.0.0.1:8765/"){UseShellExecute=true});
    }
    public static string Q(string s){return "\""+s.Replace("\\\"","\\\\\"").Replace("\"","\\\"").TrimEnd('\\')+"\"";}
    [STAThread]static int Main(string[] args){
        Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
        string root=Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"app");
        for(int i=0;i+1<args.Length;i++)if(args[i]=="--app-root")root=Path.GetFullPath(args[i+1]);
        if(!File.Exists(Path.Combine(root,"desktop.py"))){MessageBox.Show("The paper app is missing. Run RUG-DESK-Setup.exe first.");return 2;}
        if(Array.IndexOf(args,"--uninstall")>=0){
            if(MessageBox.Show("Disable this installation's background jobs? Open paper positions will stop being managed. All account data and program files will be preserved.","RUG DESK",MessageBoxButtons.YesNo)!=DialogResult.Yes)return 0;
            var p=Process.Start(new ProcessStartInfo(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"python/python.exe"),Q(Path.Combine(root,"desktop.py"))+" uninstall-jobs"){UseShellExecute=false,CreateNoWindow=true});p.WaitForExit();if(p.ExitCode!=0){MessageBox.Show("Could not remove jobs. Existing task ownership was preserved.");return 2;}
            Microsoft.Win32.Registry.CurrentUser.DeleteSubKeyTree(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\RugDeskPaper",false);
            MessageBox.Show("Background jobs removed. Your ledgers, inputs and application files remain at "+root);return 0;
        }
        Application.Run(new RugDesk(root));return 0;
    }
}
