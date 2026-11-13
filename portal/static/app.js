'use strict';
(() => {
  const $ = value => document.querySelector(value);
  const names = {gitlab:'代码仓库',knowledge:'设定资料室',animation:'动画资产工作台',image:'图片生成',speech:'语音制作',music:'音乐制作',model3d:'3D 模型生成',narrative:'剧情设计',effects:'特效制作'};
  const roles = {llm:'LLM / 翻译官',vision:'视觉模型',embedding:'Embedding',rerank:'Reranker',image:'图片生成',speech:'语音生成',music:'音乐生成',model3d:'3D 生成',video:'视频生成',workflow:'ComfyUI'};
  let session = null, state = null, editing = null, workspace = null, editingUser = null;
  let authPoll = null, loginPreferenceApplied = false;
  const node = (tag, text, cls) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  function notice(text) {$('#message').textContent=text||'';$('#message').hidden=!text;}
  async function api(path, method='GET', value) {
    const headers={...window.FantasyModels?.headers()};if(value!==undefined)headers['Content-Type']='application/json';
    if(method!=='GET' && session)headers['X-Community-CSRF']=session.csrf;
    const response=await fetch(path,{method,headers,credentials:'same-origin',cache:'no-store',...(value!==undefined?{body:JSON.stringify(value)}:{})});
    let result;try{result=await response.json();}catch{throw new Error('服务未返回有效结果');}
    if(response.status===401 && path!=='/api/login'){showLogin();throw new Error('登录已过期，请重新登录');}
    if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'操作未完成');
    return result;
  }
  function showLogin(message=''){session=null;window.FantasyModels?.setUser(null);$('#workspace').hidden=true;$('#login-view').hidden=false;$('#login-status').textContent=message;$('#login-status').hidden=!message;}
  async function showWorkspace(){
    clearTimeout(authPoll);authPoll=null;
    window.FantasyModels?.setUser(session.user.id);
    $('#login-view').hidden=true;$('#workspace').hidden=false;$('#user-name').textContent=session.user.name+(session.user.admin?' · 管理员':' · 成员');
    document.querySelectorAll('.admin-only').forEach(el=>{el.hidden=!session.user.admin;});await refresh();
    if(location.pathname==='/logout')notice('点击右上角“退出登录”即可安全结束本次会话。');
  }
  async function refresh(){
    const results = await Promise.allSettled([api('/api/profiles'),api('/api/workspace')]);
    if(!session)return;
    if(results[0].status==='fulfilled'){state=results[0].value;render();}
    if(results[1].status==='fulfilled'){workspace=results[1].value;renderWorkspace();}
    else{workspace=null;renderWorkspace();}
    const failed=results.filter(result=>result.status==='rejected');
    if(failed.length)notice(failed.map(result=>result.reason.message).join('；'));
  }
  function renderWorkspace(){
    const labels={ready:'可用',not_configured:'待配置',unavailable:'暂不可用',unknown:'待确认'};
    document.querySelectorAll('.module-card[data-module]').forEach(card=>{
      const info=workspace?.modules?.[card.dataset.module]||{},status=info.status||'unknown';
      let badge=card.querySelector('.module-status');
      if(!badge){badge=node('span',undefined,'module-status');card.prepend(badge);}
      badge.textContent=info.allowed===false?'暂无权限':labels[status]||'待确认';
      badge.dataset.status=info.allowed===false?'unavailable':status;
      badge.title=info.detail||'';
      const link=card.querySelector('a');
      const features=Object.entries(info.features||{}).filter(([key])=>!['management','prompt_preview'].includes(key));
      let capabilityNote=card.querySelector('.module-capabilities');
      if(!capabilityNote){capabilityNote=node('p',undefined,'module-capabilities footnote');card.insertBefore(capabilityNote,link);}
      capabilityNote.hidden=!features.length;
      if(features.length){
        const available=features.filter(([,feature])=>feature.available).length;
        capabilityNote.textContent=info.local_available===false?'模块服务未启动':`基础功能可用；AI 功能 ${available}/${features.length} 项可用`;
        capabilityNote.title=features.map(([,feature])=>`${feature.label}：${feature.available?'可用':'待配置 '+feature.missing_roles.map(value=>value.split('|').map(role=>roles[role]||role).join(' 或 ')).join('、')}`).join('\n');
      }
      link.setAttribute('aria-disabled',String(info.allowed===false));
      if(info.allowed===false)link.removeAttribute('href');
      else link.href=card.dataset.module==='gitlab'?'/gitlab/':`/${card.dataset.module}/`;
    });
    $('#link-gitlab-button').hidden=!(workspace?.repository?.oauth_enabled&&session?.user.auth_provider==='local');
    $('#password-button').textContent=session?.user.auth_provider==='gitlab'?'GitLab 密码':'修改密码';
  }
  document.addEventListener('click',event=>{
    const link=event.target.closest('a[href]');
    if(!link||new URL(link.href,location.href).pathname!=='/gitlab/')return;
    const theme=window.FantasyTheme?.getDefault();
    if(theme==='light'||theme==='dark')link.href='/gitlab/?fantasy_theme='+theme;
  },true);
  function button(text,handler,cls){const b=node('button',text,cls);b.type='button';b.addEventListener('click',handler);return b;}
  function render(){
    const grid=$('#profiles');grid.replaceChildren();
    if(!state.profiles.length)grid.append(node('p','还没有模型配置。添加 API 连接并通过类型检测后，即可在下方选择用途。','empty-state'));
    for(const profile of state.profiles){
      const card=node('article',undefined,'profile-card'),head=node('div',undefined,'profile-head');
      head.append(node('h3',profile.name),node('span',profile.verified?'检测通过':'待检测',`pill${profile.verified?' verified':''}`));card.append(head);
      card.append(node('p',`${profile.scope==='personal'?'个人 API · 仅自己可见':'全局 API · 全员可用'} · ${roles[profile.kind]} · ${profile.protocol}${profile.capabilities.includes('music_train')?' · 支持音乐训练':''}`),node('p',profile.model||'ComfyUI 工作流服务'),node('p',profile.base_url),node('p',profile.has_api_key?'API Key：已加密保存':'API Key：未设置'));
      if(profile.can_manage){
        const actions=node('div',undefined,'actions');
        actions.append(button('检测能力',async event=>{event.currentTarget.disabled=true;event.currentTarget.textContent='正在检测…';notice('正在使用小型探测请求验证 API 的真实能力。');try{await api(`/api/profiles/${profile.id}/verify`,'POST',{allow_generation_probe:$('#allow-generation-probe').checked});notice(`${profile.name}：能力检测通过。`);}catch(error){notice(error.message);}finally{await refresh();}}),button('编辑',()=>openProfile(profile)),button('删除',async()=>{if(!confirm(`删除“${profile.name}”及其模块绑定？`))return;try{await api(`/api/profiles/${profile.id}`,'DELETE');await refresh();notice('配置已删除。');}catch(error){notice(error.message);}},'danger'));card.append(actions);
      }grid.append(card);
    }
    const bindingGrid=$('#bindings');bindingGrid.replaceChildren();
    for(const [module,moduleRoles] of Object.entries(state.module_roles)){
      const card=node('article',undefined,'binding-card');card.append(node('h3',names[module]));
      for(const role of moduleRoles){
        const roleLabel=module==='animation'&&role==='vision'?'LLM辅助与参考图理解（需识图）':module==='animation'&&role==='image'?'背景去除 · 图片 API（必需）':roles[role];
        const label=node('label',roleLabel),select=node('select');select.setAttribute('aria-label',`${names[module]} ${roleLabel}`);
        const bound=(state.default_bindings||state.bindings).find(b=>b.module===module&&b.role===role);
        const defaultProfile=state.profiles.find(p=>p.id===bound?.profile_id);
        select.append(new Option(`跟随全局默认${defaultProfile?'：'+defaultProfile.name:'（未配置）'}`,'__default__'),new Option('此浏览器不使用',''));
        for(const profile of state.profiles.filter(p=>p.verified&&p.capabilities.includes(role)))select.append(new Option(`${profile.name} · ${profile.scope==='personal'?'个人':'全局'}`,profile.id));
        const chosen=window.FantasyModels?.selection(module,role);
        if(chosen&&!Array.from(select.options).some(o=>o.value===chosen)){const stale=state.profiles.find(p=>p.id===chosen);const option=new Option(`${stale?stale.name:'原配置'}（不可用，请重新选择）`,chosen);option.disabled=true;select.append(option);}
        select.value=chosen===undefined?'__default__':chosen||'';
        select.addEventListener('change',async()=>{window.FantasyModels.set(module,role,select.value==='__default__'?undefined:select.value||null);await refresh();notice(`${names[module]}的${roleLabel}已保存到当前账号的浏览器设置。`);});
        label.append(select);card.append(label);
        if(session.user.admin){const globalSelect=node('select');globalSelect.setAttribute('aria-label',`${names[module]} ${roleLabel} 全局默认`);globalSelect.append(new Option('全局默认：未配置',''));for(const profile of state.profiles.filter(p=>p.scope==='global'&&p.verified&&p.capabilities.includes(role)))globalSelect.append(new Option('全局默认：'+profile.name,profile.id));globalSelect.value=defaultProfile?.verified?defaultProfile.id:'';globalSelect.addEventListener('change',async()=>{try{await api(`/api/bindings/${module}/${role}`,'PUT',{profile_id:globalSelect.value||null});await refresh();notice('全局默认已更新。各账号已保存的浏览器选择继续生效。');}catch(error){notice(error.message);}});card.append(globalSelect);}
      }if(module==='animation')card.append(node('p','辅助模式会读取参考图并细化动作指令，请选择通过 Vision 能力检测的模型。直接提示词模式无需此配置。','footnote'));bindingGrid.append(card);
    }
  }
  function openProfile(profile){
    editing=profile;const form=$('#profile-form');form.reset();form.querySelector('.form-error').textContent='';$('#profile-title').textContent=profile?'编辑模型配置':'添加模型配置';
    if(profile)for(const key of ['name','kind','protocol','base_url','model'])form.elements[key].value=profile[key];
    form.elements.scope.value=profile?.scope||'personal';form.elements.scope.disabled=!!profile||!session.user.admin;
    form.elements.scope.querySelector('[value="global"]').disabled=!session.user.admin;
    form.elements.options.value=JSON.stringify(profile?profile.options:{},null,2);$('#profile-dialog').showModal();
  }
  async function formTask(form,task){const error=form.querySelector('.form-error')||$('#login-error'),submit=form.querySelector('[type=submit]');error.textContent='';submit.disabled=true;try{await task();}catch(e){error.textContent=e.message;}finally{submit.disabled=false;}}
  $('#login-form').addEventListener('submit',event=>{event.preventDefault();formTask(event.currentTarget,async()=>{const form=event.currentTarget;session=await api('/api/login','POST',{username:form.elements.username.value,password:form.elements.password.value});form.elements.password.value='';await showWorkspace();});});
  $('#logout-button').addEventListener('click',async()=>{try{await api('/api/logout','POST',{});showLogin('社区工作台已退出。如果 GitLab 网页仍保持登录，请在 GitLab 中另行退出。');}catch(e){notice(e.message);}});
  $('#refresh').addEventListener('click',()=>refresh().catch(e=>notice(e.message)));
  $('#add-profile').addEventListener('click',()=>openProfile(null));
  $('#profile-form').addEventListener('submit',event=>{event.preventDefault();const form=event.currentTarget;formTask(form,async()=>{let options;try{options=JSON.parse(form.elements.options.value||'{}');}catch{throw new Error('高级参数不是有效 JSON');}const data={};for(const key of ['name','kind','protocol','base_url','model','scope'])data[key]=form.elements[key].value;data.options=options;if(!editing||form.elements.api_key.value||form.elements.clear_key.checked)data.api_key=form.elements.clear_key.checked?'':form.elements.api_key.value;await api(editing?`/api/profiles/${editing.id}`:'/api/profiles',editing?'PUT':'POST',data);$('#profile-dialog').close();await refresh();notice('配置已保存。请先点击“检测能力”，再选择模块用途。');});});
  $('#password-button').addEventListener('click',()=>{if(session?.user.auth_provider==='gitlab'){notice('此账号由 GitLab 登录，请进入代码仓库，在 GitLab 的账号设置中修改密码。');return;}$('#password-form').reset();$('#password-form .form-error').textContent='';$('#password-dialog').showModal();});
  $('#password-form').addEventListener('submit',event=>{event.preventDefault();const form=event.currentTarget;formTask(form,async()=>{const result=await api('/api/password','POST',{old_password:form.elements.old_password.value,new_password:form.elements.new_password.value});session.csrf=result.csrf;$('#password-dialog').close();form.reset();notice('密码已更新，其他登录已失效。');});});
  async function renderUsers(){
    const result=await api('/api/users');
    const gitlabManaged=result.management?.provider==='gitlab';
    $('#user-form').hidden=gitlabManaged;
    $('#users-list').replaceChildren(...result.users.map(user=>{
      const row=node('article',undefined,'user-row');
      row.append(node('p',`${user.name} · ${user.username}`),node('small',`${user.admin?'管理员':'成员'} · ${user.active===false?'已停用':'可登录'}${user.auth_provider==='gitlab'?' · GitLab 账号':''}`));
      const actions=node('div',undefined,'actions');
      if(user.auth_provider==='gitlab'){
        const link=node('a','在 GitLab 中管理');link.href=result.management?.url||'/gitlab/';link.target='_blank';link.rel='noopener';actions.append(link);row.append(actions);return row;
      }
      actions.append(button('管理',()=>{
        editingUser=user;$('#edit-user-title').textContent=`管理账号 · ${user.name}`;
        const form=$('#edit-user-form');form.reset();form.elements.name.value=user.name;form.elements.admin.checked=user.admin;form.elements.active.checked=user.active!==false;
        form.elements.admin.disabled=form.elements.active.disabled=user.id===session.user.id;
        $('#reset-user-form').reset();$('#reset-user-form').hidden=user.id===session.user.id||user.auth_provider==='gitlab';
        document.querySelectorAll('#edit-user-dialog .form-error').forEach(el=>el.textContent='');$('#edit-user-dialog').showModal();
      }));
      if(user.id!==session.user.id)actions.append(button(user.active===false?'恢复登录':'停用',async()=>{try{await api(`/api/users/${user.id}`,'PATCH',{active:user.active===false});await renderUsers();}catch(error){notice(error.message);}}),button('删除',async()=>{
        if(!confirm(`删除账号“${user.name}”？该账号将无法登录，作品保留原账号归属。重新创建同名账号不会继承作品。需要以后恢复访问时，请选择停用。`))return;
        try{await api(`/api/users/${user.id}`,'DELETE');await renderUsers();}catch(error){notice(error.message);}
      },'danger'));
      row.append(actions);return row;
    }));
    if(gitlabManaged){
      const help=node('p','账号创建、停用、资料及管理员权限由 GitLab 管理。下方为已登录过平台的账号，权限在登录和会话复验时同步。');
      const link=node('a','打开 GitLab 用户管理');link.href=result.management.url;link.target='_blank';link.rel='noopener';
      $('#users-list').prepend(help,link);
    }
  }
  $('#users-button').addEventListener('click',async()=>{try{await renderUsers();$('#user-form').reset();$('#user-form .form-error').textContent='';$('#users-dialog').showModal();}catch(e){notice(e.message);}});
  $('#user-form').addEventListener('submit',event=>{event.preventDefault();const form=event.currentTarget;formTask(form,async()=>{await api('/api/users','POST',{username:form.elements.username.value,password:form.elements.password.value,name:form.elements.name.value,admin:form.elements.admin.checked});form.reset();await renderUsers();});});
  $('#edit-user-form').addEventListener('submit',event=>{event.preventDefault();const form=event.currentTarget;formTask(form,async()=>{
    await api(`/api/users/${editingUser.id}`,'PATCH',{name:form.elements.name.value,admin:form.elements.admin.checked,active:form.elements.active.checked});
    if(editingUser.id===session.user.id){session.user.name=form.elements.name.value.trim();$('#user-name').textContent=session.user.name+' · 管理员';}
    $('#edit-user-dialog').close();await renderUsers();
  });});
  $('#reset-user-form').addEventListener('submit',event=>{event.preventDefault();const form=event.currentTarget;formTask(form,async()=>{
    await api(`/api/users/${editingUser.id}/password`,'POST',{new_password:form.elements.new_password.value});form.reset();$('#edit-user-dialog').close();await renderUsers();notice('密码已重置，该账号原有登录已退出。');
  });});
  const repositoryForm=$('#repository-form');
  const managedLabel=node('label',undefined,'checkbox'),managedChoice=node('input');
  managedChoice.type='checkbox';managedChoice.name='use_managed_defaults';managedChoice.checked=true;
  managedLabel.append(managedChoice,document.createTextNode('使用随附 GitLab 的自动登录配置'));
  repositoryForm.querySelector('details').before(managedLabel);
  const updateRepositoryFields=()=>{
    managedLabel.hidden=repositoryForm.elements.mode.value!=='managed';
    const automatic=!managedLabel.hidden&&managedChoice.checked;
    repositoryForm.elements.url.readOnly=automatic;
    for(const name of ['oauth_enabled','oauth_client_id','oauth_client_secret','clear_client_secret'])repositoryForm.elements[name].disabled=automatic;
  };
  managedChoice.addEventListener('change',updateRepositoryFields);
  repositoryForm.elements.mode.addEventListener('change',()=>{
    if(repositoryForm.elements.mode.value==='managed')managedChoice.checked=true;
    updateRepositoryFields();
  });
  $('#repository-button').addEventListener('click',()=>{
    const form=repositoryForm,value=workspace?.repository||{};form.reset();form.elements.mode.value=['managed','external','disabled'].includes(value.mode)?value.mode:'managed';form.elements.url.value=value.url||'';form.elements.oauth_client_id.value=value.oauth_client_id||'';form.elements.oauth_enabled.checked=!!value.oauth_enabled;form.elements.callback_url.value=value.callback_url||new URL('/auth/gitlab/callback',location.href).href;managedChoice.checked=value.managed_defaults??value.mode==='managed';updateRepositoryFields();form.querySelector('.form-error').textContent='';$('#repository-dialog').showModal();
  });
  $('#repository-form').addEventListener('submit',event=>{event.preventDefault();const form=event.currentTarget;formTask(form,async()=>{
    const automatic=form.elements.mode.value==='managed'&&managedChoice.checked;
    const value=automatic?{mode:'managed',use_managed_defaults:true}:{mode:form.elements.mode.value,use_managed_defaults:false,url:form.elements.mode.value==='disabled'?'':form.elements.url.value,oauth_client_id:form.elements.oauth_client_id.value,oauth_enabled:form.elements.mode.value!=='disabled'&&form.elements.oauth_enabled.checked,clear_client_secret:form.elements.clear_client_secret.checked};
    if(!automatic&&form.elements.oauth_client_secret.value)value.oauth_client_secret=form.elements.oauth_client_secret.value;
    await api('/api/repository','PUT',value);form.elements.oauth_client_secret.value='';$('#repository-dialog').close();await refresh();await refreshAuthOptions();notice('代码仓库配置已保存。');
  });});
  $('#link-gitlab-button').addEventListener('click',async()=>{try{const result=await api('/auth/gitlab/link-start','POST',{});const target=new URL(result.authorization_url);if(!['http:','https:'].includes(target.protocol))throw new Error('GitLab 登录地址无效');location.assign(target.href);}catch(error){notice(error.message);}});
  document.querySelectorAll('dialog .close').forEach(el=>el.addEventListener('click',()=>el.closest('dialog').close()));
  api(location.pathname==='/logout'?'/api/logout-context':'/api/session').then(async value=>{session=value;await showWorkspace();}).catch(()=>showLogin());
  async function refreshAuthOptions(){
    try{
      const response=await fetch('/api/auth/options',{credentials:'same-origin',cache:'no-store'});
      if(!response.ok)return;
      const value=await response.json(),gitlab=$('#gitlab-login'),form=$('#login-form');
      let local=$('#local-login-details');
      if(!local){
        local=node('details');local.id='local-login-details';local.open=true;
        local.append(node('summary','使用本地账号（维护入口）'));
        form.before(local);local.append(form);
        local.before(gitlab);
      }
      gitlab.hidden=!(value.gitlab_login||value.gitlab_initializing||value.gitlab_setup_error);
      local.hidden=value.local_login===false;
      gitlab.classList.toggle('primary',value.preferred_login==='gitlab');
      gitlab.textContent=value.gitlab_setup_error?'GitLab 初始化需检查':value.gitlab_initializing?'GitLab 正在初始化…':'使用 GitLab 统一登录';
      gitlab.setAttribute('aria-disabled',String(!value.gitlab_login));
      if(value.gitlab_login)gitlab.href='/auth/gitlab/start';else gitlab.removeAttribute('href');
      if(!loginPreferenceApplied||!value.gitlab_login){
        local.open=value.preferred_login!=='gitlab'||!value.gitlab_login;
        if(value.gitlab_login||!value.gitlab_initializing)loginPreferenceApplied=true;
      }
      let status=$('#gitlab-setup-status');
      if(!status){status=node('p',undefined,'footnote');status.id='gitlab-setup-status';gitlab.after(status);}
      status.hidden=!(value.gitlab_initializing||value.gitlab_setup_error);
      status.textContent=value.gitlab_setup_error?'GitLab 自动配置暂未完成，请检查仓库部署配置和容器日志。':'首次启动正在准备代码仓库和统一登录。';
      clearTimeout(authPoll);authPoll=null;
      if(value.gitlab_initializing&&!session)authPoll=setTimeout(refreshAuthOptions,5000);
    }catch{ /* The local maintenance form remains available. */ }
  }
  refreshAuthOptions();
})();
