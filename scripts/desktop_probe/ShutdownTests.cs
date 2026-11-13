// SPDX-License-Identifier: MIT
using System.Text.Json;

namespace Qiban.DesktopProbe;
internal static class ShutdownTests
{
    internal static int Run(string directory)
    {
        Directory.CreateDirectory(directory);var cases=new List<string>();
        void Require(bool value){if(!value)throw new Exception("Shutdown journal assertion failed");}
        ShutdownJournal Open(string name)=>new(Path.Combine(directory,name));
        try
        {
            var first=Open("clean");first.Begin();Require(!first.NeedsRecovery);first.Verified();first.Closing();first.Complete(true);
            var clean=Open("clean");clean.Begin();Require(!clean.NeedsRecovery);clean.Complete(true);
            cases.Add("clean_shutdown_does_not_claim_abnormal_exit");
            var crash=Open("crash");crash.Begin();crash.Verified();
            var recovered=Open("crash");recovered.Begin();Require(recovered.NeedsRecovery);recovered.Verified();recovered.Closing();recovered.Complete(true);
            var checkedAgain=Open("crash");checkedAgain.Begin();Require(!checkedAgain.NeedsRecovery);
            cases.Add("interrupted_run_requires_check_and_success_clears_pending_recovery");
            var forced=Open("forced");forced.Begin();forced.Closing();forced.Abnormal(true);forced.Complete(true);
            var afterForce=Open("forced");afterForce.Begin();Require(afterForce.NeedsRecovery);afterForce.Closing();afterForce.Complete(true);
            var failedRecovery=Open("forced");failedRecovery.Begin();Require(failedRecovery.NeedsRecovery);
            cases.Add("forced_exit_and_unverified_recovery_never_mark_clean");
            var failure=Open("failed");failure.Begin();failure.Verified();failure.Abnormal();failure.Closing();failure.Complete(true);
            var afterFailure=Open("failed");afterFailure.Begin();Require(afterFailure.NeedsRecovery);
            cases.Add("prior_component_failure_survives_later_cleanup");
            var bad=Path.Combine(directory,"invalid");Directory.CreateDirectory(bad);File.WriteAllText(Path.Combine(bad,"application-session.json"),"{");
            bool rejected=false;try{Open("invalid").Begin();}catch(IOException){rejected=true;}
            Require(rejected && File.ReadAllText(Path.Combine(bad,"application-session.json"))=="{");
            cases.Add("damaged_journal_is_preserved_and_blocks_false_clean_start");
            File.WriteAllText(Path.Combine(directory,"shutdown-result.json"),JsonSerializer.Serialize(new{passed=true,cases}));return 0;
        }
        catch(Exception error){File.WriteAllText(Path.Combine(directory,"shutdown-result.json"),JsonSerializer.Serialize(new{passed=false,cases,error=error.Message}));return 1;}
    }
}
