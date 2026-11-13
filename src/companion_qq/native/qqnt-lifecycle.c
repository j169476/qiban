// SPDX-License-Identifier: MIT
// Qiban single-environment probe host lifecycle. No guessed Node private layouts.
// Explicit host shutdown plus Node-API cleanup; not a general QQ runtime replacement.
#include <windows.h>
#include <stdbool.h>
#include <string.h>
#include "node_api.h"
static volatile LONG stopping = 1;
static void *owner_isolate;
static napi_env owner_env;
static void *(*current_isolate)(void);
static napi_status (*add_cleanup)(node_api_basic_env, napi_cleanup_hook, void *);
static napi_status (*make_function)(napi_env,const char *,size_t,napi_callback,void *,napi_value *);
static napi_status (*set_named)(napi_env,napi_value,const char *,napi_value);
static napi_status (*make_boolean)(napi_env,bool,napi_value *);
static void fail(const char *message) {
 DWORD written; WriteFile(GetStdHandle(STD_ERROR_HANDLE),message,(DWORD)lstrlenA(message),&written,NULL); ExitProcess(74);
}
static void cleanup(void *arg) {
 InterlockedExchange(&stopping,1);
 const char message[]="HOST_CLEANUP_STOPPING=true\n";DWORD written;
 WriteFile(GetStdHandle(STD_ERROR_HANDLE),message,sizeof(message)-1,&written,NULL);
}
bool qiban_is_stopping(void *isolate) {
 if (!owner_isolate || isolate != owner_isolate) fail("UNOWNED_ISOLATE\n");
 return InterlockedCompareExchange(&stopping,0,0)!=0;
}
static napi_value query(napi_env env,napi_callback_info info) {
 if(env!=owner_env)fail("UNOWNED_ENVIRONMENT\n");
 napi_value result;
 if(make_boolean(env,qiban_is_stopping(current_isolate()),&result)!=napi_ok)fail("BOOLEAN_FAILED\n");
 return result;
}
static napi_value begin_shutdown(napi_env env,napi_callback_info info) {
 if(env!=owner_env)fail("UNOWNED_ENVIRONMENT\n");
 InterlockedExchange(&stopping,1);
 return query(env,info);
}
__declspec(dllexport) napi_value napi_register_module_v1(napi_env env,napi_value exports) {
 if(owner_env)fail("MULTIPLE_ENVIRONMENTS_UNSUPPORTED\n");
 HMODULE host=GetModuleHandleW(NULL);
 #define RESOLVE(target,name) do { FARPROC address=GetProcAddress(host,name); memcpy(&target,&address,sizeof(target)); if(!target)fail("MISSING_HOST_API: " name "\n"); } while(0)
 RESOLVE(current_isolate,"?GetCurrent@Isolate@v8@@SAPEAV12@XZ");
 RESOLVE(add_cleanup,"napi_add_env_cleanup_hook");
 RESOLVE(make_function,"napi_create_function");
 RESOLVE(set_named,"napi_set_named_property");
 RESOLVE(make_boolean,"napi_get_boolean");
 owner_isolate=current_isolate();owner_env=env;
 if(!owner_isolate)fail("NO_CURRENT_ISOLATE\n");
 if(add_cleanup(env,cleanup,NULL)!=napi_ok)fail("CLEANUP_REGISTRATION_FAILED\n");
 napi_value fn;
 if(make_function(env,"isStopping",NAPI_AUTO_LENGTH,query,NULL,&fn)!=napi_ok || set_named(env,exports,"isStopping",fn)!=napi_ok)fail("QUERY_REGISTRATION_FAILED\n");
 if(make_function(env,"beginShutdown",NAPI_AUTO_LENGTH,begin_shutdown,NULL,&fn)!=napi_ok || set_named(env,exports,"beginShutdown",fn)!=napi_ok)fail("SHUTDOWN_REGISTRATION_FAILED\n");
 InterlockedExchange(&stopping,0);
 return exports;
}
