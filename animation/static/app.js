const $ = q => document.querySelector(q), $$ = q => [...document.querySelectorAll(q)];
const state = {catalog:null, jobs:[], assets:[], notifications:[], selectedJob:null, studioRevision:0, generationRevision:0, character:null, logged:false, user:null, permissions:{}, epoch:0, refresh:0, pendingDeletions:new Map()};
Object.assign(state,{referenceCharacter:null,referenceMain:null,referenceEpoch:0,additionalFacings:[],additionalUploads:[],referenceUploadPending:0});
const DEFAULT_MAX_REFERENCE_IMAGES=9;
const labels = {queued:'排队中',compiling:'编译 Prompt',submitting:'提交生成',polling:'生成中',generating:'生成中',postprocessing:'后处理',completed:'已完成',failed:'失败',cancelled:'已取消',needs_attention:'需要处理',retry_wait:'等待重试',running:'执行中'};
const deletableJobs = new Set(['completed','failed','cancelled']);
function node(tag,text,cls){const e=document.createElement(tag);if(text!=null)e.textContent=text;if(cls)e.className=cls;return e}
function toast(text){$('#toast').textContent=text;$('#toast').classList.add('visible');setTimeout(()=>$('#toast').classList.remove('visible'),6500)}
function empty(element,text){element.replaceChildren(node('div',text,'empty'))}
function clearPersonal(){
  state.epoch++;state.studioRevision++;state.jobs=[];state.assets=[];state.notifications=[];state.selectedJob=null;
  if(state.referenceUploadPending){state.referenceEpoch++;state.referenceUploadPending=0;$('#additional-reference-files').value='';$('#additional-reference-feedback').textContent='上传结果未加入当前草稿，请重新选择需要的图片。';renderAdditionalReferences()}
  renderJobs();renderAssets();renderNotifications();$('#asset-detail').replaceChildren();delete $('#asset-detail').dataset.job;$('#asset-dialog').close();
  $('#result-stage').replaceChildren();$('#result-stage').classList.add('hidden');delete $('#result-stage').dataset.asset;delete $('#result-stage').dataset.jobVideo;
  $('#reference-stage').classList.remove('hidden');$('#studio-status').textContent='等待任务';$('#studio-detail').textContent='选择角色后，实际使用的主参考图会显示在这里。';$('#prompt-preview').textContent='点击提示词预览或校验，查看本次视频生成的实际输入。';$('#batch-result').textContent='';
}
function clearSession(){
  state.logged=false;state.user=null;state.permissions={};state.catalog=null;state.character=null;
  clearAdditionalReferences();renderAdditionalReferences();
  state.generationRevision++;renderGenerationMessage('');
  state.pendingDeletions.clear();renderDeletionStatus();
  $$('.scope-toggle, .admin-tool').forEach(e=>e.classList.add('hidden'));$$('[data-scope]').forEach(e=>e.checked=true);
  clearPersonal();$('#current-user').textContent='';$('#character-list').replaceChildren();$('#motion-list').replaceChildren();
  $('#asset-search').value='';$('#toast').textContent='';$('#toast').classList.remove('visible');
  $('#character-editor').classList.add('hidden');$('#character-create').classList.add('hidden');$('#profile-editor').value='';
  $('#character-select').replaceChildren();$('#main-reference-select').replaceChildren();delete $('#main-reference-select').dataset.character;$('#character-images').replaceChildren();$('#motion-select').replaceChildren();$('#reference-img').removeAttribute('src');$('#reference-img').hidden=true;
  for(const id of ['task-form','character-create','motion-create','reference-update'])$('#'+id).reset();
  renderPromptMode();
  resetBatchExample();
  $('#studio-detail').textContent='登录后显示当前用户的工作空间。';$('#login').classList.remove('hidden');
}
async function api(path,body,method){
  const epoch=state.epoch;
  const options={credentials:'same-origin',method:method||(body?'POST':'GET')};
  if(body instanceof FormData)options.body=body;else if(body!==undefined){options.body=JSON.stringify(body);options.headers={'Content-Type':'application/json'}}
  const response=await fetch(path,options);const data=await response.json().catch(()=>({detail:response.statusText}));
  if(!response.ok){if(response.status===401&&epoch===state.epoch){clearSession();if(window.FANTASY_SSO)window.location.assign(window.FANTASY_SSO.login)}const error=new Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail||data.error||data));error.status=response.status;if(data.code==='generation_services_not_ready'){error.code=data.code;error.ready=false;error.missingServices=Array.isArray(data.missing_services)?data.missing_services.filter(id=>['video','image','vision'].includes(id)):[]}throw error}return data;
}
function renderGenerationMessage(message){for(const id of ['generation-readiness','batch-readiness']){const el=$('#'+id);el.textContent=message;el.classList.toggle('hidden',!message)}}
function generationUnavailableMessage(missing,providers={}){
  const names={video:'视频 API',image:'抠图 API',vision:'识图 LLM'};
  return missing.map(id=>{const name=names[id],status=providers[id]?.state||providers[id]?.status;if(status==='starting')return `${name} 正在启动，请等待就绪`;if(status==='stopped')return `${name} 已停止，请检查主页模型 API 配置`;if(status==='error')return `${name} 启动失败，请到主页检查配置`;return `${name} 尚未就绪，请在主页配置模型 API`}).join('。')||'暂时无法确认生成服务状态，请到主页检查视频 API 与抠图 API。';
}
function renderGenerationHealth(health,revision=state.generationRevision){
  if(revision!==state.generationRevision)return;
  const matte=health?.matting;
  $('#generation-flow').textContent=!matte?'生成流程：视频 API → 透明背景处理 → 透明帧与精灵图。正在检查背景处理配置。':matte.mode==='api'?(matte.available?`生成流程：视频 API → ${matte.name} 逐帧抠图 → 透明帧与精灵图。每个保留帧调用一次图片 API，可能逐帧计费；请先少帧预览并核对细节。`:matte.message):'请在主页绑定视频 API 和支持透明背景图片编辑的图片 API，完成后才可生成透明帧与精灵图。';
  if(!health){$('#health').textContent='○ 连接暂时中断';renderGenerationMessage('暂时无法确认生成服务状态，请稍后重试或到主页检查视频 API 与抠图 API。');return}
  const required=health.providers?.vision ? ['video','image','vision'] : ['video','image'];
  const missing=required.filter(id=>health.providers?.[id]?.available!==true);
  const ready=health.generation_ready===true&&missing.length===0;
  if(ready){renderGenerationMessage('');$('#health').textContent=health.database&&health.redis?'● 队列、视频与抠图 API 就绪':'○ 模型 API 已配置 · 队列连接待检查'}
  else{renderGenerationMessage(health.generation_message||generationUnavailableMessage(missing,health.providers));$('#health').textContent='● 档案管理可用 · 部分生成 API 尚未就绪'}
}
async function submitGeneration(path,payload){
  const epoch=state.epoch;
  try{const result=await api(path,payload);if(epoch===state.epoch){state.generationRevision++;renderGenerationMessage('')}return result}
  catch(error){if(epoch===state.epoch&&error.code==='generation_services_not_ready'){state.generationRevision++;renderGenerationMessage(error.message||generationUnavailableMessage(error.missingServices));$('#health').textContent='○ 视频或抠图 API 尚未就绪'}throw error}
}
function items(data){return Array.isArray(data)?data:data.items||data.jobs||data.assets||data.notifications||data.characters||data.motions||[]}
function link(text,url){const a=node('a',text,'button');a.href=url;a.target='_blank';a.rel='noopener';return a}
function fileURL(path){if(!path)return '';if(path.startsWith('/animation/files/'))return path;if(path.startsWith('/files/'))return '/animation'+path;return '/animation/files/'+path.replace(/^\/+/, '')}
function ownerText(value){return `创建者：${value.owner_name||value.owner_id||'未标注'}${value.owner_id?'（'+value.owner_id+'）':''}`}
function canEdit(value){return state.user?.role==='admin'||value.owner_id===state.user?.id}
function scopePath(kind){return '/api/'+kind+'?all_users='+String($(`#${kind}-all-users`).checked)}
function renderDeletionStatus(){
  const el=$('#deletion-status');el.replaceChildren();el.classList.toggle('hidden',!state.pendingDeletions.size);
  for(const [key,pending] of state.pendingDeletions){
    const row=node('div',null,'deletion-pending');row.append(node('strong','文件清理未完成'),node('p',`记录 ${pending.label} 已移除，部分文件尚待删除。可重试清理；重复点击不会删除其他记录。`));
    const retry=node('button','重试文件清理','danger');retry.type='button';retry.onclick=()=>guarded(retry,async()=>{
      const epoch=state.epoch,result=await api(`/api/${pending.kind}/${encodeURIComponent(pending.id)}`,undefined,'DELETE');if(epoch!==state.epoch)return;
      if(!result.cleanup_pending)state.pendingDeletions.delete(key);renderDeletionStatus();await refresh();
      toast(result.cleanup_pending?'文件清理仍未完成，请检查服务器日志后重试。':'文件清理已完成。');
    });row.append(retry);el.append(row);
  }
}
async function session(){
  const epoch=state.epoch,value=await api('/api/auth/session');if(epoch!==state.epoch)throw new Error('登录状态或显示范围已变更，请重试。');
  const changed=JSON.stringify([state.user,state.permissions])!==JSON.stringify([value.user,value.permissions]);
  if(changed)clearSession();
  state.user=value.user;state.permissions=value.permissions||{};state.logged=true;
  $('#current-user').textContent=`${state.user.name||state.user.id} · ${state.user.role==='admin'?'管理员':'用户'}`;
  $$('.scope-toggle').forEach(e=>e.classList.toggle('hidden',!state.permissions.can_view_all));
  $$('.admin-tool').forEach(e=>e.classList.toggle('hidden',state.user.role!=='admin'));
  $('#login').classList.add('hidden');return changed;
}
function promptMode(){return new FormData($('#task-form')).get('prompt_mode')==='direct'?'direct':'assisted'}
function renderPromptMode(){
  const direct=promptMode()==='direct';
  $('#direct-prompt-controls').classList.toggle('hidden',!direct);$('#direct-service-help').classList.toggle('hidden',!direct);
  $('#assisted-profile-controls').classList.toggle('hidden',direct);$('#assisted-prompt-controls').classList.toggle('hidden',direct);
  $('#video-prompt').disabled=!direct;$('#video-prompt').required=direct;
  $('#use-character-description').disabled=direct;$('#supplementary-description').disabled=direct;
  $('#compile').textContent=direct?'预览实际视频提示词':'预览 Prompt';
  $('#mode-production-help').textContent=direct?'每次提交生成一个任务。动作原文保留，工作台自动附加纯灰背景、固定镜头、完整主体等约束，生成后去除背景并导出透明帧与精灵图。正式生产前请确认角色档案。':'识图 LLM 结合参考图的姿态和朝向，把动作意图展开成具体指令；工作台固定附加灰底、固定镜头、完整主体等约束。标签只用于查找图片。正式生产前请确认档案。';
}
function task(){
  if(state.referenceUploadPending)throw new Error('补充图片正在上传，请等待上传完成后再预览或生成。');
  const f=new FormData($('#task-form')),mode=promptMode(),videoPrompt=mode==='direct'?String(f.get('video_prompt')||''):'';
  if(mode==='direct'&&!videoPrompt.trim())throw new Error('请填写视频动作提示词。');
  if(videoPrompt.length>12000)throw new Error('视频提示词最多 12000 字符。');
  const primary=mainReferenceFacing();if(!primary)throw new Error('当前档案没有可用图片，请先新增图片。');
  return {character_id:f.get('character_id'),reference_id:primary,prompt_mode:mode,video_prompt:videoPrompt,additional_reference_ids:[...state.additionalFacings],additional_reference_upload_ids:state.additionalUploads.map(image=>image.id),use_character_description:mode==='assisted'&&f.has('use_character_description'),motion_id:f.get('motion_id')||null,supplementary_description:mode==='assisted'?f.get('supplementary_description')||'':'',duration:Number(f.get('duration')),seed:Number(f.get('seed')),preview:f.has('preview'),root_motion:false,camera:{mode:'static'},loop:true,target:{fps:Number(f.get('fps')),frame_width:Number(f.get('size')),frame_height:Number(f.get('size')),anchor:'feet_center'},postprocessing:{remove_background:true,align_anchor:f.has('align_anchor'),frame_selection:'uniform'}}
}
function taskLabel(task){return [task.character_id||'角色',task.action,task.facing,!task.action&&!task.facing?(task.prompt_mode==='direct'?'直接视频提示词':'AI 辅助编写'):null].filter(Boolean).join(' / ')}
function hasGeneratedVideo(job){return Boolean(job.checkpoint?.video)}
function jobVideoURL(job){return `/animation/files/jobs/${encodeURIComponent(job.id)}/generation.mp4`}
function jobErrorText(job){return typeof job.error==='string'?job.error:job.error?.message||''}
function jobBackgroundMessage(job){if(job.stage!=='postprocessing')return '';const frames=job.checkpoint?.matting_frames||[];return frames.length?`背景处理：已完成 ${frames.filter(frame=>frame.state==='completed').length} / ${frames.length} 帧`:''}
function postprocessingFailed(job){return job.status==='failed'&&job.stage==='postprocessing'&&hasGeneratedVideo(job)}
function backgroundRemovalFailed(job){return postprocessingFailed(job)&&/(background|matt(?:e|ing)|alpha|背景|抠图)/i.test(jobErrorText(job))}
function jobStatusText(job){
  if(backgroundRemovalFailed(job))return '视频已生成，背景去除失败';
  if(postprocessingFailed(job))return '视频已生成，透明资产处理失败';
  if(job.stage==='postprocessing'&&['queued','running','postprocessing'].includes(job.status)){
    const frames=job.checkpoint?.matting_frames||[];const mattingStatus=frames.length?(frames.every(frame=>frame.state==='completed')?'succeeded':'running'):'queued';
    if(mattingStatus==='queued')return '等待背景处理';
    if(mattingStatus==='running')return '正在去背景';
    if(mattingStatus==='succeeded')return '正在导出透明帧与精灵图';
    if(hasGeneratedVideo(job))return '正在制作透明动画资产';
  }
  return labels[job.status]||job.status;
}
function jobFailureMessage(job){
  if(backgroundRemovalFailed(job))return '视频生成已成功，但背景去除失败。原视频已保留为中间产物；透明帧与精灵图尚未完成。';
  if(postprocessingFailed(job))return '视频生成已成功，但透明动画资产处理失败。原视频已保留为中间产物，可查看错误详情。';
  if(job.status==='failed'&&['polling','generating','submitting','generated'].includes(job.stage)&&!hasGeneratedVideo(job))return '视频生成未完成：'+jobErrorText(job);
  return job.error?jobErrorText(job):'';
}
function canReprocess(job){return canEdit(job)&&postprocessingFailed(job)&&job.checkpoint?.h3_terminal==='completed'}
function rawVideoDownload(job){const a=link('下载原视频（中间产物）',jobVideoURL(job));a.download='generation.mp4';return a}
function reprocessButton(job){
  if(!canReprocess(job))return null;
  const button=node('button','重新处理背景');button.type='button';
  button.onclick=()=>guarded(button,async()=>{const epoch=state.epoch,result=await api(`/api/jobs/${encodeURIComponent(job.id)}/reprocess`,{});if(epoch!==state.epoch)return;state.jobs=state.jobs.map(value=>value.id===job.id?result:value);if($('#asset-detail').dataset.job===job.id&&$('#asset-dialog').open)showJobDialog(result);toast('已重新加入背景处理队列，复用现有视频，不重新生成。');await refresh()});
  return button;
}
function intermediateVideo(job,container){
  const note=node('p','中间产物：视频 API 原始视频，尚未去除背景；最终交付仍需完成透明帧与精灵图。','intermediate-note');
  const video=node('video');video.controls=true;video.preload='metadata';video.src=jobVideoURL(job);
  const actions=node('div',null,'form-actions');actions.append(rawVideoDownload(job));const retry=reprocessButton(job);if(retry)actions.append(retry);
  container.append(note,video,actions);
}
function showJobDialog(job){
  const target=$('#asset-detail');target.replaceChildren(node('h2','任务详情'),node('p',taskLabel(job.task||job)),node('p',jobStatusText(job),'pill'));target.dataset.job=job.id;
  const failure=jobFailureMessage(job);if(failure)target.append(node('p',failure,'processing-error'));
  const background=jobBackgroundMessage(job);if(background)target.append(node('p',background,'intermediate-note'));
  if(hasGeneratedVideo(job))intermediateVideo(job,target);
  const details=node('details');details.append(node('summary','查看任务与错误详情'),node('pre',JSON.stringify(job,null,2)));target.append(details);if(!$('#asset-dialog').open)$('#asset-dialog').showModal();
}
function showStudioIntermediate(job){
  const result=$('#result-stage');$('#reference-stage').classList.add('hidden');result.classList.remove('hidden');
  // Do not restart video playback on every status poll; only action availability changes.
  if(result.dataset.jobVideo!==job.id){result.replaceChildren();delete result.dataset.asset;result.dataset.jobVideo=job.id;intermediateVideo(job,result)}
  else{const old=result.querySelector?.('.form-actions');if(old){const actions=node('div',null,'form-actions');actions.append(rawVideoDownload(job));const retry=reprocessButton(job);if(retry)actions.append(retry);old.replaceWith(actions)}}
}
function clearAdditionalReferences(){state.referenceEpoch++;state.referenceCharacter=null;state.referenceMain=null;state.additionalFacings=[];state.additionalUploads=[];state.referenceUploadPending=0;$('#additional-reference-files').value='';$('#additional-reference-feedback').textContent=''}
function referenceCharacter(){return state.catalog?.characters.find(c=>c.id===$('#character-select').value)}
function referenceViews(character){return {...character?.views,...character?.view_urls}}
function characterImages(character){return character?.images||Object.entries(referenceViews(character)).map(([id,path])=>({id,label:character?.reference_labels?.[id]??id,path,url:path}))}
function referenceLabel(character,id){return characterImages(character).find(image=>image.id===id)?.label||'未标注'}
function mainReferenceFacing(character=referenceCharacter()){const selector=$('#main-reference-select'),views=referenceViews(character);return selector.dataset.character===character?.id&&views[selector.value]?selector.value:Object.keys(views)[0]||null}
function syncAdditionalReferences(){
  const character=referenceCharacter(),id=character?.id||null,selector=$('#main-reference-select'),selected=mainReferenceFacing(character);
  if(selector.dataset.character!==id)selector.value='';
  fill(selector,characterImages(character).map((image,index)=>({id:image.id,name:`${image.label||'未标注'} · 图片 ${index+1}`})),selected);
  selector.dataset.character=id||'';selector.disabled=!characterImages(character).length;
  const main=mainReferenceFacing(character);
  let changed=false;if(id!==state.referenceCharacter){clearAdditionalReferences();state.referenceCharacter=id;changed=true}
  const views=referenceViews(character),valid=state.additionalFacings.filter(facing=>facing!==main&&views[facing]);
  const signature=JSON.stringify([main,views[main]||null]);if(state.referenceMain!==signature){state.referenceMain=signature;changed=true}
  if(valid.length!==state.additionalFacings.length){state.additionalFacings=valid;changed=true}
  return changed;
}
function additionalReferenceCount(){return state.additionalFacings.length+state.additionalUploads.length}
function additionalReferenceLimit(){const limits=state.catalog?.reference_limits||{},total=Number.isInteger(limits.max_images)&&limits.max_images>0?limits.max_images:DEFAULT_MAX_REFERENCE_IMAGES,additional=Number.isInteger(limits.max_additional_images)&&limits.max_additional_images>=0?limits.max_additional_images:total-1;return Math.min(additional,total-1)}
function referenceFeedback(message){$('#additional-reference-feedback').textContent=message;toast(message)}
function setAdditionalFacing(facing,checked){
  const character=referenceCharacter(),views=referenceViews(character);
  if(!character||facing===mainReferenceFacing(character)||!views[facing])return;
  if(checked&&!state.additionalFacings.includes(facing)){
    if(additionalReferenceCount()+state.referenceUploadPending>=additionalReferenceLimit()){referenceFeedback(`最多添加 ${additionalReferenceLimit()} 张补充图片（含档案图片和上传图片），请先移除一张。`);renderAdditionalReferences();return}
    state.additionalFacings.push(facing);
  }else if(!checked)state.additionalFacings=state.additionalFacings.filter(value=>value!==facing);
  $('#additional-reference-feedback').textContent='';resetStudioDraft();
}
function removeAdditionalUpload(id){state.additionalUploads=state.additionalUploads.filter(image=>image.id!==id);$('#additional-reference-feedback').textContent='已取消本次图片选择，服务器上的上传源文件仍保留。';resetStudioDraft()}
function renderAdditionalReferences(){
  const character=referenceCharacter(),views=referenceViews(character),main=mainReferenceFacing(character),total=additionalReferenceCount(),limit=additionalReferenceLimit();
  const saved=$('#additional-saved-references'),uploaded=$('#additional-uploaded-references');saved.replaceChildren();uploaded.replaceChildren();
  $('#additional-reference-count').textContent=`已选 ${total} / ${limit} 张补充图片${state.referenceUploadPending?' · 上传中 '+state.referenceUploadPending+' 张':''}`;
  $('#additional-reference-guidance').textContent=promptMode()==='direct'?`主参考图为图片 1，补充图片按选择顺序一起提交。动作按你的提示词执行，工作台附加透明动画的基本约束。最多添加 ${limit} 张。`:`主参考图始终为图片 1，决定观看角度。补充图只补充角色外观，不改变主图角度；最多添加 ${limit} 张。`;
  for(const facing of Object.keys(views)){
    if(facing===main||!views[facing])continue;
    const label=node('label',null,'reference-choice'),input=node('input'),img=node('img'),selected=state.additionalFacings.indexOf(facing);
    input.type='checkbox';input.value=facing;input.checked=selected>=0;input.disabled=selected<0&&total+state.referenceUploadPending>=limit;
    input.onchange=event=>{event?.stopPropagation();if(character.id!==state.referenceCharacter)return;setAdditionalFacing(facing,input.checked)};
    const caption=referenceLabel(character,facing);img.src=fileURL(views[facing]);img.alt=`${character.name||character.id} · ${caption} 补充参考图`;img.loading='lazy';
    label.append(img,node('span',selected>=0?`${caption} · 补充 ${selected+1}`:caption),input);saved.append(label);
  }
  if(!saved.children.length)empty(saved,character?'当前档案暂无其他图片，可直接上传补充图片。':'请先选择角色。');
  for(const [index,image] of state.additionalUploads.entries()){
    const card=node('div',null,'reference-upload-card'),img=node('img'),remove=node('button','移除');
    img.src=image.url;img.alt=image.name;img.loading='lazy';remove.type='button';remove.onclick=()=>removeAdditionalUpload(image.id);
    card.append(img,node('span',`补充 ${state.additionalFacings.length+index+1} · ${image.name}`),remove);uploaded.append(card);
  }
  const disabled=!character||state.referenceUploadPending>0||total>=limit;
  $('#additional-reference-files').disabled=disabled;$('#upload-additional-references').disabled=disabled;
}
async function uploadAdditionalReferences(){
  const input=$('#additional-reference-files'),files=[...(input.files||[])],character=referenceCharacter();
  if(!character){referenceFeedback('请先选择角色，再上传补充图片。');return}
  if(state.referenceUploadPending){referenceFeedback('补充图片正在上传，请等待完成。');return}
  if(!files.length){referenceFeedback('请先选择需要上传的图片。');return}
  if(additionalReferenceCount()+files.length>additionalReferenceLimit()){referenceFeedback(`最多添加 ${additionalReferenceLimit()} 张补充图片（含档案图片和上传图片），请减少选择。`);return}
  if(files.some(file=>!file.size||file.size>200*1024*1024||! /\.(png|jpe?g|webp)$/i.test(file.name))){referenceFeedback('请选择有效的 PNG、JPEG 或 WebP 图片；单张不能为空且不能超过 200 MB。');return}
  const epoch=state.epoch,referenceEpoch=state.referenceEpoch,id=character.id;
  const current=()=>epoch===state.epoch&&referenceEpoch===state.referenceEpoch&&id===state.referenceCharacter&&id===$('#character-select').value&&state.logged;
  state.referenceUploadPending=files.length;input.value='';$('#additional-reference-feedback').textContent='正在上传补充图片…';resetStudioDraft();
  try{
    for(const file of files){
      const image=await upload(file,'character');if(!current())return;
      if(typeof image.id!=='string'||!image.id||(!image.url&&!image.path))throw new Error('图片上传结果不完整，请重试。');
      state.referenceUploadPending--;if(!state.additionalUploads.some(value=>value.id===image.id))state.additionalUploads.push({id:image.id,url:fileURL(image.url||image.path),name:file.name});
      resetStudioDraft();
    }
    if(current())$('#additional-reference-feedback').textContent='补充图片已选入本次任务。移除只取消选择，不删除上传源文件。';
  }catch(error){if(current())referenceFeedback([400,413,422].includes(error.status)?'图片校验未通过：请使用有效的 PNG、JPEG 或 WebP，单张不超过 2500 万像素和服务器上传大小限制。':'补充图片上传失败：'+error.message)}
  finally{if(current()){state.referenceUploadPending=0;renderAdditionalReferences()}}
}
function fill(select,values,value){const prior=select.value;select.replaceChildren(...values.map(v=>{const o=node('option',typeof v==='string'?v:v.name||v.id);o.value=typeof v==='string'?v:v.id;return o}));const options=[...select.options];if(options.some(o=>o.value===prior))select.value=prior;else if(options.some(o=>o.value===value))select.value=value}
function reference(){const c=referenceCharacter(),id=mainReferenceFacing(c),path=referenceViews(c)[id];$('#reference-img').src=fileURL(path);$('#reference-img').hidden=!path;$('#studio-detail').textContent=c?`${c.name||c.id} · ${c.approved?'已确认角色':'草稿角色'} · 主参考图片：${id?referenceLabel(c,id):'尚无图片'}`:'暂无角色，请先创建角色档案';renderAdditionalReferences()}
function resetStudioDraft(){
  renderPromptMode();
  syncAdditionalReferences();
  state.studioRevision++;state.selectedJob=null;
  const result=$('#result-stage');result.replaceChildren();result.classList.add('hidden');delete result.dataset.asset;delete result.dataset.jobVideo;
  $('#reference-stage').classList.remove('hidden');$('#studio-status').textContent='等待任务';
  $('#prompt-preview').textContent='任务设置已改变，请重新预览 Prompt 或生成动画。';reference();
}
async function catalog(){const epoch=state.epoch,c=await api('/api/catalog');if(epoch!==state.epoch||!state.logged)return;state.catalog=c;const selectors=['#character-select','#motion-select'],prior=selectors.map(q=>$(q).value);fill($('#character-select'),c.characters||[]);fill($('#motion-select'),[{id:'',name:'不使用参考视频'},...(c.motions||[])]);const referencesChanged=syncAdditionalReferences();if(referencesChanged||selectors.some((q,i)=>$(q).value!==prior[i]))resetStudioDraft();else reference();renderCharacters();renderMotions()}
async function refresh(){
  if(!state.logged)return;
  const ticket=++state.refresh;await session();if(ticket!==state.refresh)return;
  if(!state.catalog)await catalog();const epoch=state.epoch,generationRevision=state.generationRevision;
  const results=await Promise.allSettled([api(scopePath('jobs')),api(scopePath('assets')),api(scopePath('notifications')),api('/api/health?prompt_mode='+promptMode())]);
  if(epoch!==state.epoch||ticket!==state.refresh||!state.logged)return;
  for(const [index,kind,render] of [[0,'jobs',renderJobs],[1,'assets',renderAssets],[2,'notifications',renderNotifications]]){
    if(results[index].status==='fulfilled'){state[kind]=items(results[index].value);render()}
    else {state[kind]=[];render();toast(results[index].reason.message)}
  }
  renderGenerationHealth(results[3].status==='fulfilled'?results[3].value:null,generationRevision);
  if(state.selectedJob){const j=state.jobs.find(j=>j.id===state.selectedJob);if(j){$('#studio-status').textContent=jobStatusText(j);$('#studio-detail').textContent=`Job ${j.id} · ${ownerText(j)} · ${labels[j.stage]||j.stage||j.status}${j.task?.supplementary_description?' · 补充：'+j.task.supplementary_description:''}${jobFailureMessage(j)?' · '+jobFailureMessage(j):''}${jobBackgroundMessage(j)?' · '+jobBackgroundMessage(j):''}`;if(j.status==='completed'&&j.asset_id){if($('#result-stage').dataset.asset!==j.asset_id)await showStudioAsset(j.asset_id)}else if(hasGeneratedVideo(j))showStudioIntermediate(j)}}
}
function deleteButton(kind,record){
  if(!state.permissions.can_delete||!canEdit(record))return null;
  const button=node('button','删除','danger');button.type='button';
  if(kind==='jobs'&&!deletableJobs.has(record.status)){button.disabled=true;button.title='仅可删除已完成、失败或已取消的任务；正在运行、排队及需要处理的任务必须先处理。';return button}
  const names={characters:'角色档案',motions:'动作参考',jobs:'任务',assets:'游戏资产',notifications:'通知'};
  button.onclick=()=>guarded(button,async()=>{
    const effects={assets:'将永久删除资产记录、原始视频、PNG 序列、Sprite Sheet、预览与 ZIP 文件。',jobs:'将永久删除任务记录及该任务的临时文件。已归档的游戏资产独立保留。',characters:'将永久删除角色档案及该角色登记的、未被其他资料共用的参考文件。仍被任务使用的角色需要先处理相关任务。',motions:'将永久删除动作参考记录及未被其他资料共用的视频和分析文件。仍被任务使用的参考需要先处理相关任务。',notifications:'将永久删除这条通知。'};
    if(!window.confirm(`确定删除${names[kind]}“${record.name||record.title||record.id}”？\n${effects[kind]}\n此操作不可撤销。`))return;
    const epoch=state.epoch,result=await api(`/api/${kind}/${encodeURIComponent(record.id)}`,undefined,'DELETE');if(epoch!==state.epoch)return;
    const key=kind+'/'+record.id;if(result.cleanup_pending)state.pendingDeletions.set(key,{kind,id:record.id,label:record.name||record.title||record.id});else state.pendingDeletions.delete(key);renderDeletionStatus();
    clearPersonal();if(kind==='characters'||kind==='motions'){if(state.character===record.id){state.character=null;$('#character-editor').classList.add('hidden')}await catalog()}
    await refresh();toast(result.cleanup_pending?`${names[kind]}记录已移除，但文件清理尚未完成，请管理员检查服务器清理记录。`:`${names[kind]}已删除${result.retained_shared_paths?.length?'；其他资料共用的参考文件已保留。':''}`);
  });return button;
}
function appendDelete(container,kind,value){const button=deleteButton(kind,value);if(button)container.append(button)}
function renderCharacters(){const el=$('#character-list');el.replaceChildren();for(const c of state.catalog?.characters||[]){const card=node('div',null,'card');const img=node('img');img.alt=c.name||c.id;img.src=fileURL(characterImages(c)[0]?.url||characterImages(c)[0]?.path);card.append(img);const body=node('div',null,'card-body');body.append(node('h3',c.name||c.id),node('p',`${characterImages(c).length} 张图片 · ${c.approved?'已确认 · 可正式生产':'草稿 · 需人工确认'}`),node('p',ownerText(c),'owner'));const btn=node('button',canEdit(c)?'编辑与确认档案':'查看档案');btn.onclick=()=>editCharacter(c.id).catch(e=>toast(e.message));body.append(btn);appendDelete(body,'characters',c);card.append(body);el.append(card)}if(!el.children.length)empty(el,'还没有角色。上传一张参考图开始。')}
function renderCharacterImages(c){
  const gallery=$('#character-images');gallery.replaceChildren();
  for(const [index,image] of characterImages(c).entries()){
    const card=node('div',null,'profile-image-card'),preview=link('',fileURL(image.url||image.path)),img=node('img');
    img.src=fileURL(image.url||image.path);img.alt=`${c.name||c.id} · ${image.label||'未标注'} · 图片 ${index+1}`;img.loading='lazy';preview.className='profile-image-preview checker';preview.append(img);
    card.append(preview,node('strong',image.label||'未标注'),node('small',`图片 ${index+1}`));
    if(canEdit(c)){
      const label=node('input'),save=node('button','保存标签');label.value=image.label||'';label.maxLength=100;label.placeholder='图片标签';label.setAttribute?.('aria-label',`图片 ${index+1} 的标签`);save.type='button';
      save.onclick=()=>guarded(save,async()=>{const epoch=state.epoch;await api(`/api/characters/${encodeURIComponent(c.id)}/references/${encodeURIComponent(image.id)}`,{label:label.value},'PATCH');if(epoch!==state.epoch)return;await catalog();if(state.character===c.id)await editCharacter(c.id);toast('标签已保存')});card.append(label,save);
      const remove=node('button','删除图片','danger');remove.type='button';remove.onclick=()=>guarded(remove,async()=>{
        if(!window.confirm(`确定从“${c.name||c.id}”删除图片 ${index+1}（标签：${image.label||'未标注'}）？未被其他档案共用的源图片将从硬盘删除，已生成任务中的独立参考副本不受影响。`))return;
        const epoch=state.epoch,result=await api(`/api/characters/${encodeURIComponent(c.id)}/references/${encodeURIComponent(image.id)}`,undefined,'DELETE');if(epoch!==state.epoch)return;
        await catalog();if(state.character===c.id)await editCharacter(c.id);
        toast(result.cleanup_pending?'图片已从档案移除，服务器文件清理待重试。':result.retained_shared_paths?.length?'图片已从档案移除；其他资料共用的源文件已保留。':'图片及未共用的源文件已删除。');
      });card.append(remove);
    }
    gallery.append(card);
  }
  if(!gallery.children.length)empty(gallery,'档案中还没有图片，请在下方新增。');
}
async function editCharacter(id){const epoch=state.epoch,c=await api('/api/characters/'+encodeURIComponent(id));if(epoch!==state.epoch)return;state.character=id;$('#character-editor-name').textContent=`${c.name||id} · ${ownerText(c)}`;$('#profile-editor').value=JSON.stringify(c.profile||{},null,2);const editable=canEdit(c);$('#profile-editor').readOnly=!editable;$('#analyze-character').hidden=!editable;$('#approve-character').hidden=!editable;$('#reference-update').classList.toggle('hidden',!editable);renderCharacterImages(c);$('#character-editor').classList.remove('hidden');$('#character-editor').scrollIntoView({behavior:'smooth',block:'center'})}
function renderMotions(){const el=$('#motion-list');el.replaceChildren();for(const m of state.catalog?.motions||[]){const card=node('div',null,'card-body panel');card.append(node('h3',m.name||m.id),node('p',m.description||'动作参考'),node('p',ownerText(m),'owner'));if(m.url||m.path)card.append(link('查看参考视频',fileURL(m.url||m.path)));appendDelete(card,'motions',m);el.append(card)}if(!el.children.length)empty(el,'尚未上传动作参考。也可以直接使用文字动作生成。')}
function renderJobs(){const el=$('#job-list');el.replaceChildren();$('#job-count').textContent=state.jobs.filter(j=>!['completed','failed','cancelled','needs_attention'].includes(j.status)).length||'';for(const j of state.jobs){const row=node('div',null,'job-row'),a=node('div',taskLabel(j.task||j));a.append(node('small',j.id),node('small',ownerText(j),'owner'));row.append(a,node('span',jobStatusText(j),'pill'),node('span',labels[j.stage]||j.stage||''));const actions=node('div',null,'job-actions');if(j.asset_id){const b=node('button','预览 / 导出');b.onclick=()=>assetDialog(j.asset_id).catch(e=>toast(e.message));actions.append(b)}if(hasGeneratedVideo(j)){const b=node('button','预览原视频');b.onclick=()=>showJobDialog(j);actions.append(b,rawVideoDownload(j))}const backgroundRetry=reprocessButton(j);if(backgroundRetry)actions.append(backgroundRetry);if(canEdit(j)&&j.status==='failed'&&j.retryable&&!postprocessingFailed(j)){const b=node('button','重试');b.onclick=()=>api(`/api/jobs/${j.id}/retry`,{}).then(refresh).catch(e=>toast(e.message));actions.append(b)}if(canEdit(j)&&!['completed','failed','cancelled','needs_attention'].includes(j.status)){const b=node('button','取消');b.onclick=()=>api(`/api/jobs/${j.id}/cancel`,{}).then(refresh).catch(e=>toast(e.message));actions.append(b)}const detail=node('button','详情');detail.onclick=()=>showJobDialog(j);actions.append(detail);appendDelete(actions,'jobs',j);row.append(actions);el.append(row)}if(!el.children.length)empty(el,'当前范围暂无任务。生成后会在这里显示状态。')}
function renderAssets(){const el=$('#asset-list');el.replaceChildren();const query=$('#asset-search').value.toLowerCase();for(const a of state.assets.filter(a=>JSON.stringify(a).toLowerCase().includes(query))){const card=node('div',null,'card');if(a.files?.preview||a.files?.spritesheet){const img=node('img');img.src=fileURL(a.files.preview||a.files.spritesheet);img.alt='动画资产预览';card.append(img)}const body=node('div',null,'card-body');body.append(node('h3',`${a.character_id||a.task?.character_id||'角色'} · ${a.action||a.task?.action||(a.task?.prompt_mode==='direct'?'直接提示词':'动画')}`),node('p',`${a.facing||a.task?.facing||''} · ${a.preview?'预览资产':'生产资产'}`),node('p',a.id),node('p',ownerText(a),'owner'));const b=node('button','预览与导出 →');b.onclick=()=>assetDialog(a.id).catch(e=>toast(e.message));body.append(b);appendDelete(body,'assets',a);card.append(body);el.append(card)}if(!el.children.length)empty(el,'当前范围暂无匹配的资产。完成的任务会自动归档。')}
function renderNotifications(){const el=$('#notification-list');el.replaceChildren();$('#notice-count').textContent=state.notifications.length||'';for(const n of state.notifications){const card=node('div',null,'notice');card.append(node('strong',n.title||n.kind||'任务通知'),node('p',n.message||n.body||JSON.stringify(n.payload||{})),node('p',ownerText(n).replace('创建者：',n.job_id?'归属用户（任务创建人）：':'归属用户：'),'owner'),node('small',n.created_at||''));appendDelete(card,'notifications',n);el.append(card)}if(!el.children.length)empty(el,'当前范围暂无通知。批次完成或失败时会在此显示。')}
function media(asset,container){container.replaceChildren();if(asset.files?.preview){const wrap=node('div',null,'checker');const img=node('img');img.src=fileURL(asset.files.preview);img.alt='透明动画预览';wrap.append(img);container.append(wrap)}if(asset.files?.video){const video=node('video');video.controls=true;video.loop=true;video.src=fileURL(asset.files.video);container.append(video)}const links=node('div',null,'form-actions');links.append(link('下载完整资产 ZIP',`/animation/api/assets/${asset.id}/export.zip`));if(asset.files?.spritesheet)links.append(link('Sprite Sheet',fileURL(asset.files.spritesheet)));if(asset.files?.metadata)links.append(link('Metadata',fileURL(asset.files.metadata)));if(asset.files?.manifest)links.append(link('帧清单',fileURL(asset.files.manifest)));container.append(links);if(asset.quality||asset.warnings)container.append(node('pre',JSON.stringify(asset.quality||asset.warnings,null,2)))}
async function assetDialog(id){const epoch=state.epoch,a=await api('/api/assets/'+id);if(epoch!==state.epoch)return;const el=$('#asset-detail');delete el.dataset.job;el.replaceChildren(node('h2','动画资产'),node('p',ownerText(a),'owner'));if(a.task?.supplementary_description)el.append(node('p','补充描述：'+a.task.supplementary_description));const body=node('div');media(a,body);el.append(body);const detail=node('details');detail.append(node('summary','查看完整追溯信息'),node('pre',JSON.stringify(a,null,2)));el.append(detail);$('#asset-dialog').showModal()}
async function showStudioAsset(id){const epoch=state.epoch,revision=state.studioRevision,job=state.selectedJob,a=await api('/api/assets/'+id);if(epoch!==state.epoch||revision!==state.studioRevision||job!==state.selectedJob)return;$('#reference-stage').classList.add('hidden');const el=$('#result-stage');el.classList.remove('hidden');el.dataset.asset=id;delete el.dataset.jobVideo;media(a,el);el.append(node('p',`本次结果：${taskLabel(a.task||a)}`,'hint'));if(typeof a.task?.use_character_description==='boolean')el.append(node('p',a.task.use_character_description?'本次使用角色文字档案':'本次仅使用已选参考图片','hint'))}
async function guarded(button,work){button.disabled=true;try{await work()}catch(e){toast(e.message)}finally{button.disabled=false}}
async function upload(file,kind){const form=new FormData();form.append('file',file);form.append('kind',kind);return api('/api/uploads',form)}
$('#login-form').onsubmit=e=>{e.preventDefault();window.location.assign('/login')};
$('#home').href=window.FANTASY_SSO?.home||'/';
$('#logout').onclick=()=>{const destination=window.FANTASY_SSO?.logout||'/';clearSession();window.location.assign(destination)};
$$('[data-scope]').forEach(input=>input.onchange=()=>{clearPersonal();refresh().catch(e=>toast(e.message))});
$$('nav button').forEach(b=>b.onclick=()=>{$$('nav button').forEach(n=>n.classList.toggle('active',n===b));$$('.page').forEach(p=>p.classList.toggle('hidden',p.id!==b.dataset.page));$('#page-title').textContent=b.textContent.replace(/[◈◉↗≋▦⇉○\d]/g,'').trim();refresh().catch(e=>toast(e.message))});
$('#refresh').onclick=()=>guarded($('#refresh'),async()=>{await session();await catalog();await refresh();toast('已刷新')});$('#task-form').onchange=e=>{if(e?.target?.id!=='additional-reference-files')resetStudioDraft()};$('#asset-search').oninput=renderAssets;
$('#compile').onclick=()=>guarded($('#compile'),async()=>{const epoch=state.epoch,revision=state.studioRevision,payload=task(),result=await api('/api/compile',payload);if(epoch!==state.epoch||revision!==state.studioRevision)return;$('#prompt-preview').textContent=(payload.prompt_mode==='direct'?'直接视频模式：动作提示词原文保留；以下为工作台附加纯灰背景、固定镜头、完整主体等约束后的实际提示词（未调用 LLM）':`AI 辅助编写：识图 LLM 已结合参考图理解动作意图，并生成具体动作指令。${payload.use_character_description?'角色外观：参考图片与文字档案':'角色外观：仅使用已选参考图片'}`)+'\n\n'+result.prompt+'\n\n'+JSON.stringify({task:payload,h3_request:result.h3_request,warnings:result.warnings},null,2);$('#prompt-preview').parentElement.open=true;toast(payload.prompt_mode==='direct'?'已预览实际视频提示词：动作原文保留，工作台附加基本约束，未调用 LLM':'Prompt 已编译并校验')});
$('#task-form').onsubmit=e=>{e.preventDefault();guarded($('#generate'),async()=>{const epoch=state.epoch,revision=state.studioRevision,payload=task(),result=await submitGeneration('/api/animation/generate',{...payload,idempotency_key:crypto.randomUUID()});if(epoch!==state.epoch)return;const job=result.id||result.job_id;if(revision===state.studioRevision){resetStudioDraft();state.selectedJob=job;$('#studio-status').textContent='已入队';$('#studio-detail').textContent=`${taskLabel(payload)} · 已入队`;}toast(`单项任务已入队：${job}`);await refresh()})};
$('#upload-additional-references').onclick=()=>uploadAdditionalReferences();
$('#add-character').onclick=()=>$('#character-create').classList.toggle('hidden');
$('#character-create').onsubmit=e=>{e.preventDefault();guarded(e.submitter,async()=>{const epoch=state.epoch,f=new FormData(e.target),files=f.getAll('reference').filter(file=>file.size),images=[];if(!files.length)throw new Error('请至少选择一张图片');if(files.length>200)throw new Error('每个档案最多保存 200 张图片');for(const file of files){const result=await upload(file,'character-reference');if(epoch!==state.epoch)return;images.push({label:f.get('label')||'',path:result.path})}await api('/api/characters',{id:f.get('id'),name:f.get('name'),images,profile:{description:''}});if(epoch!==state.epoch)return;e.target.reset();e.target.classList.add('hidden');await catalog();toast('带标注的图片档案已创建')})};
$('#reference-update').onsubmit=e=>{e.preventDefault();guarded(e.submitter,async()=>{const epoch=state.epoch,id=state.character,f=new FormData(e.target),files=f.getAll('reference').filter(file=>file.size);if(!files.length)throw new Error('请选择新增图片');for(const file of files){const u=await upload(file,'character-reference');if(epoch!==state.epoch)return;await api('/api/characters/'+encodeURIComponent(id)+'/references',{label:f.get('label')||'',path:u.path});if(epoch!==state.epoch)return}e.target.reset();await catalog();if(state.character===id)await editCharacter(id);toast('图片已新增；相同标签的现有图片会保留，请重新核对档案')})};
$('#analyze-character').onclick=()=>guarded($('#analyze-character'),async()=>{const epoch=state.epoch,id=state.character,r=await api(`/api/characters/${id}/analyze`,{});if(epoch!==state.epoch||state.character!==id)return;$('#profile-editor').value=JSON.stringify(r.profile||r,null,2);await catalog();toast('视觉模型分析完成，请核对并确认')});
$('#approve-character').onclick=()=>guarded($('#approve-character'),async()=>{const profile=JSON.parse($('#profile-editor').value);await api(`/api/characters/${state.character}/approve`,{profile});await catalog();toast('角色档案已确认')});
$('#motion-create').onsubmit=e=>{e.preventDefault();guarded(e.submitter,async()=>{const epoch=state.epoch,f=new FormData(e.target),u=await upload(f.get('file'),'motion');if(epoch!==state.epoch)return;await api('/api/motions',{name:f.get('name'),description:f.get('description'),upload_id:u.id});if(epoch!==state.epoch)return;e.target.reset();await catalog();toast('动作参考已保存')})};
function resetBatchExample(){$('#batch-id').value='batch-'+new Date().toISOString().replace(/[:.]/g,'-');$('#batch-json').value=JSON.stringify([{character_id:state.catalog?.characters?.[0]?.id||'your_character_id',prompt_mode:'assisted',video_prompt:'',duration:4,seed:0,use_character_description:false,additional_reference_ids:[],additional_reference_upload_ids:[],supplementary_description:'角色原地挥手，保持参考图中的观看角度。',preview:true,target:{fps:12,frame_width:512,frame_height:512,anchor:'feet_center'}}],null,2)}
resetBatchExample();
$('#batch-form').onsubmit=e=>{e.preventDefault();guarded(e.submitter,async()=>{const tasks=JSON.parse($('#batch-json').value);if(!Array.isArray(tasks)||!tasks.length||tasks.length>10)throw new Error('每批需要 1～10 个任务');const epoch=state.epoch,r=await submitGeneration('/api/batches',{batch_id:$('#batch-id').value,tasks});if(epoch!==state.epoch)return;$('#batch-result').textContent=JSON.stringify(r,null,2);toast('批次已入队，进度可在队列中查看');await refresh()})};
$('.dialog-close').onclick=()=>$('#asset-dialog').close();
try{await session();await catalog();await refresh()}catch(e){clearSession()}setInterval(()=>refresh().catch(()=>{}),5000);
