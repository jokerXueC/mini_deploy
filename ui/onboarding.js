window.ProjectWizard = (() => {
  let active = false, busy = false, step = 0, generation = 0;
  let detection = null, savedKey = '', prepared = false, preview = '', entryTemplate = '';
  let entryChoices = [];
  let dockerChoices = [], dockerMode = '';
  let localProjects = [], localProject = null;
  const moved = [], hidden = [];
  const originalConfirmation = $('projectConfigConfirmed').closest('label').lastChild.textContent;
  const originalValidation = $('projectForm').noValidate;
  const groups = {
    wizardConnectFields: ['projectRepoInput'],
    wizardRepositoryFields: ['projectBranchInput', 'projectProviderInput'],
    wizardRuntimeFields: ['projectTemplateInput'],
    wizardPathFields: ['projectScriptInput'],
    wizardCommandFields: ['projectBuildStepInput', 'projectRestartStepInput'],
    wizardAutomationFields: ['projectTriggerInput'],
    wizardServiceFields: ['projectServicePortInput'],
    wizardAdvancedFields: ['projectNameInput', 'projectWorkdirInput', 'projectHealthInput', 'projectStartCommandInput', 'projectKeyInput', 'projectServiceNameInput', 'projectTimeoutInput', 'projectLogInput', 'projectRollbackInput'],
  };
  const adopting = () => $('wizardSituation').value === 'existing';
  const method = () => $('wizardDeployMethod').value;
  function platformAdvice(provider = detection?.provider) {
    $('wizardPlatformAdvice').textContent = provider ? `${provider.label}：${provider.keys}` : '';
    $('wizardWebhookGuide').textContent = provider?.hooks || '在代码平台仓库设置中添加 WebHook，填写 URL 和 Secret，选择 Push 事件。';
    $('wizardWebhookModeHint').textContent = $('projectTriggerInput').value === 'manual'
      ? '当前仅手动更新，WebHook 不会触发部署。需要自动更新时，在项目设置中切换为 Push WebHook。'
      : '配置并测试 WebHook 后，指定分支的 push 才会触发更新。代码平台需能访问此地址。';
  }
  function situationFields() {
    $('wizardExistingTools').hidden = !adopting();
    $('wizardExistingDirectory').required = adopting();
    if (!adopting()) {
      if (localProject && !savedKey) {
        $('projectWorkdirInput').value = '';
        delete $('projectWorkdirInput').dataset.autoValue;
      }
      localProject = null;
    }
  }
  function fieldHelp(id, text) {
    const help = document.createElement('small');
    help.className = 'wizard-field-help wizard-added-help';
    help.id = `${id}Help`;
    help.textContent = text;
    $(id).setAttribute('aria-describedby', help.id);
    $(id).closest('label').append(help);
  }
  async function discover() {
    $('wizardDiscoverStatus').textContent = '正在查询本机 Compose 项目，不会重启服务…';
    try {
      const result = await fetchJson('projects-config/discover');
      localProjects = result.projects || [];
      $('wizardDiscoverStatus').textContent = result.notice || '未发现 Compose 项目，可手动填写。';
      $('wizardLocalProjects').innerHTML = localProjects.map((project, index) =>
        `<div class="wizard-local-project"><div><strong>${escapeHtml(project.name)}</strong><span>${escapeHtml((project.containers || []).map(item => item.name).join('、'))}</span><span>${escapeHtml(project.git_directory || project.directory)}${project.git_directory ? ' · Git 目录已核对' : ' · 待确认 Git 目录'}</span></div><button type="button" class="ghost-button compact-action" data-local-project="${index}">选择</button></div>`).join('');
    } catch (err) {
      $('wizardDiscoverStatus').textContent = '查询失败，可重试，或展开下方查询指令手动填写。';
      throw err;
    }
  }
  function selectLocalProject(index) {
    localProject = localProjects[index];
    if (!localProject) return;
    $('wizardExistingDirectory').value = localProject.git_directory || localProject.directory;
    $('projectWorkdirInput').value = $('wizardExistingDirectory').value;
    delete $('projectWorkdirInput').dataset.autoValue;
    $('projectRepoInput').value = localProject.repo || '';
    $('wizardDiscoverStatus').textContent = `已选择 ${localProject.name}。${localProject.repo ? '仓库地址已读取，请核对后检查项目。' : '未读取到可用仓库地址，请补充；代码目录也需要核对。'}`;
    $('wizardLocalProjects').querySelectorAll('button').forEach(button => {
      const selected = Number(button.dataset.localProject) === index;
      button.textContent = selected ? '已选择' : '选择';
      button.setAttribute('aria-pressed', String(selected));
    });
  }
  const error = message => {
    $('wizardError').textContent = message;
    $('wizardError').hidden = !message;
  };
  function move(node, target) {
    const marker = document.createComment('wizard field');
    node.before(marker);
    moved.push({node, marker});
    $(target).append(node);
  }
  function lock(value) {
    busy = value;
    $('projectWizard').setAttribute('aria-busy', String(value));
    $('projectWizard').querySelectorAll('input, select, textarea, button').forEach(node => { node.disabled = value; });
    $('closeProjectModalBtn').disabled = value;
    window.AppSelects?.syncAll();
  }
  function show(index) {
    step = index;
    ['wizardConnect', 'wizardConfigure', 'wizardReview'].forEach((id, i) => { $(id).hidden = i !== step; });
    document.querySelectorAll('.project-wizard-steps li').forEach((node, i) => {
      node.classList.toggle('active', i === step);
      if (i === step) node.setAttribute('aria-current', 'step');
      else node.removeAttribute('aria-current');
    });
    $('wizardBack').hidden = step === 0;
    $('wizardFinish').hidden = !savedKey;
    $('wizardNext').hidden = prepared;
    $('wizardNext').textContent = ['检查项目', '下一步：确认执行', adopting() ? '准备并检查接入' : '部署并检查'][step];
    $('wizardDeploy').hidden = !prepared;
    $('wizardRecheck').hidden = !prepared;
    $('wizardWebhookSetup').hidden = !prepared;
    $('wizardDeploy').textContent = adopting() ? '执行一次更新' : '开始首次部署';
    $('wizardFinish').textContent = adopting() && prepared ? '完成接入（不更新服务）' : '稍后处理';
    $('projectModal').querySelector('.project-modal').scrollTop = 0;
    error('');
  }
  function restore() {
    document.querySelectorAll('.wizard-added-help').forEach(node => {
      const input = node.closest('label')?.querySelector('input, select, textarea');
      input?.removeAttribute('aria-describedby');
      node.remove();
    });
    $('projectForm').noValidate = originalValidation;
    moved.splice(0).reverse().forEach(({node, marker}) => { marker.replaceWith(node); node.hidden = false; });
    hidden.splice(0).forEach(({node, value}) => { node.hidden = value; });
    $('projectStartCommandInput').required = false;
    $('projectStartCommandInput').readOnly = false;
    ['wizardSituation', 'wizardExistingDirectory', 'wizardDeployMethod', 'projectRestartStepInput', 'projectScriptInput', 'wizardEntryKind', 'wizardEntryPath', 'wizardEntryObject', 'wizardDockerFile', 'wizardDockerContainerPort', 'wizardDockerPublishedPort', 'wizardDockerVolumes'].forEach(id => { $(id).required = false; });
    $('projectWorkdirInput').readOnly = false;
    Array.from($('projectTemplateInput').options).forEach(option => { option.disabled = false; });
    $('wizardDockerEnvironment').replaceChildren();
    $('projectKeyInput').readOnly = false;
    $('projectBranchInput').removeAttribute('list');
    $('projectConfigConfirmed').closest('label').lastChild.textContent = originalConfirmation;
  }
  function open(isNew) {
    restore();
    active = isNew;
    generation++;
    $('projectWizard').hidden = !active;
    if (!active) return;
    $('projectForm').noValidate = true;
    detection = null; savedKey = ''; prepared = false; preview = ''; entryTemplate = ''; entryChoices = [];
    dockerChoices = []; dockerMode = '';
    localProjects = []; localProject = null;
    $('wizardLocalProjects').replaceChildren();
    $('wizardDiscoverStatus').textContent = '';
    $('projectWizard').querySelectorAll('details').forEach(node => { node.open = false; });
    $('wizardSituation').value = '';
    $('wizardSituation').required = true;
    $('wizardExistingDirectory').value = '';
    situationFields();
    $('wizardDeployMethod').value = '';
    $('wizardDeployMethod').required = true;
    $('wizardDockerContainerPort').value = '';
    $('wizardDockerPublishedPort').value = '8080';
    $('wizardDockerAccess').value = 'public';
    $('wizardDockerPersist').checked = false;
    $('wizardDockerVolumes').value = '';
    $('wizardCustomCommand').checked = false;
    $('projectModalHint').textContent = '连接代码，检查配置，再部署到当前服务器。';
    Array.from($('projectForm').children).filter(node => node.id !== 'projectWizard').forEach(node => {
      hidden.push({node, value: node.hidden}); node.hidden = true;
    });
    Object.entries(groups).forEach(([target, ids]) => ids.forEach(id => move($(id).closest('label'), target)));
    fieldHelp('projectRepoInput', '复制仓库的 HTTPS 或 SSH 克隆地址，例如 https://github.com/your-name/my-app.git。');
    fieldHelp('projectBranchInput', '留空使用仓库默认分支；指定分支可填 main 或 release。');
    fieldHelp('projectWorkdirInput', '首次部署自动分配 /srv/项目名；已有项目必须使用原 Git 目录。');
    fieldHelp('projectScriptInput', '例如 deploy/deploy.sh（相对仓库根目录）。沿用原脚本，不会替你改写。');
    fieldHelp('projectBuildStepInput', '沿用项目文档中的构建步骤，例如 npm ci && npm run build；不需要构建可留空。');
    fieldHelp('projectRestartStepInput', '例如 systemctl restart my-app；my-app 必须是已有服务名。不要填写长期占用前台的 python main.py。');
    fieldHelp('projectServicePortInput', '程序实际监听的端口，例如 8000；面板不会改写业务代码中的端口。');
    fieldHelp('projectHealthInput', '可选，例如 http://127.0.0.1:8000/health，必须是业务实际存在的地址。');
    $('projectBranchInput').value = '';
    $('projectBranchInput').placeholder = '留空自动识别默认分支';
    $('projectBranchInput').setAttribute('list', 'wizardBranches');
    $('wizardBranches').replaceChildren();
    $('projectRepoInput').placeholder = 'https://gitee.com/你的账号/项目.git';
    $('projectKeyInput').value = '';
    ['wizardPlatformAdvice', 'wizardDetectedFacts', 'wizardPlanSteps', 'wizardConnectResult', 'wizardProgress', 'wizardWarnings', 'wizardSummary', 'wizardFiles'].forEach(id => $(id).replaceChildren());
    move($('projectConfigConfirmed').closest('label'), 'wizardCheckField');
    $('projectConfigConfirmed').closest('label').lastChild.textContent = ' 已核对执行步骤、目标目录和运行环境';
    $('projectConfigConfirmed').checked = false;
    lock(false);
    show(0);
  }
  function close() { active = false; generation++; restore(); $('projectWizard').hidden = true; }
  function entrySettings() {
    if (!active) return {};
    const template = $('projectTemplateInput').value;
    const deployment_plan = {situation: $('wizardSituation').value, method: method(),
      ...(method() === 'commands' ? {build: $('projectBuildStepInput').value.trim(), restart: $('projectRestartStepInput').value.trim()} : {})};
    const docker_config = template === 'docker' ? dockerSettings() : {};
    if ($('wizardCustomCommand').checked || !['python', 'go', 'java'].includes(template)) {
      return {entry_kind: '', entry_path: '', entry_object: '', docker_config, deployment_plan};
    }
    return {entry_kind: template === 'python' ? $('wizardEntryKind').value : template,
      entry_path: $('wizardEntryPath').value.trim(), entry_object: $('wizardEntryObject').value.trim(), docker_config, deployment_plan};
  }
  function environmentEntries() {
    return Array.from($('wizardDockerEnvironment').children).map(row => ({
      name: row.querySelector('[data-env-name]').value.trim(), value: row.querySelector('[data-env-value]').value,
      required: row.dataset.required === 'true',
    })).filter(entry => entry.required || entry.value);
  }
  function environmentValues() {
    return $('projectTemplateInput').value === 'docker' ? Object.fromEntries(environmentEntries().map(entry => [entry.name, entry.value])) : {};
  }
  function signature() {
    const ordered = value => Array.isArray(value) ? value.map(ordered) : value && typeof value === 'object'
      ? Object.fromEntries(Object.keys(value).sort().map(key => [key, ordered(value[key])])) : value;
    return JSON.stringify(ordered({project: collectProjectForm(), environment: environmentValues()}));
  }
  function addEnvironment(name = '', required = true, fixed = false) {
    const row = document.createElement('div');
    row.className = 'wizard-env-row';
    row.dataset.required = String(required);
    row.innerHTML = `<label>变量名称${fixed ? '（必填）' : required ? '' : '（可选）'}<input data-env-name maxlength="128" placeholder="例如 DATABASE_URL" value="${escapeHtml(name)}" ${fixed ? 'readonly' : ''}></label>` +
      `<label>变量值<input data-env-value type="password" autocomplete="new-password" maxlength="8192" ${required ? 'required' : ''}></label>` +
      `<button type="button" class="ghost-button compact-action" ${fixed ? 'hidden' : ''}>移除</button>`;
    row.querySelector('button').addEventListener('click', () => { row.remove(); $('wizardDockerEnvironmentEmpty').hidden = !!$('wizardDockerEnvironment').children.length; });
    $('wizardDockerEnvironment').append(row);
    $('wizardDockerEnvironmentEmpty').hidden = true;
  }
  function dockerSettings() {
    const config = {mode: dockerMode || 'compose', file: $('wizardDockerFile').value.trim(),
      environment: environmentEntries().map(entry => entry.name)};
    if (config.mode === 'dockerfile') Object.assign(config, {
      container_port: Number($('wizardDockerContainerPort').value), published_port: Number($('wizardDockerPublishedPort').value),
      access: $('wizardDockerAccess').value,
      volumes: $('wizardDockerPersist').checked ? $('wizardDockerVolumes').value.split(/\r?\n/).map(value => value.trim()).filter(Boolean) : [],
    });
    return config;
  }
  function dockerFields(reset = false) {
    const visible = $('projectTemplateInput').value === 'docker';
    if (reset) {
      dockerChoices = detection?.docker?.choices || (detection?.candidates?.some(item => item.template === 'docker') ? [{mode: 'compose', file: 'compose.yaml'}] : []);
      if (adopting()) dockerChoices = dockerChoices.filter(choice => choice.mode === 'compose');
      $('wizardDockerChoice').innerHTML = dockerChoices.map((choice, i) => `<option value="${i}">${escapeHtml(choice.file)}${choice.mode === 'dockerfile' ? ' · 自动补齐运行配置' : ' · 使用已有配置'}</option>`).join('') +
        '<option value="compose">其他 Compose 文件</option>' + (adopting() ? '' : '<option value="dockerfile">其他 Dockerfile</option>');
      const choice = dockerChoices[0];
      $('wizardDockerChoice').value = choice ? '0' : 'compose';
      dockerMode = choice?.mode || 'compose';
      $('wizardDockerFile').value = choice?.file || '';
      const ports = detection?.docker?.ports || [];
      $('wizardDockerContainerPort').value = ports.length === 1 ? String(ports[0]) : '';
      $('wizardDockerPortHint').textContent = ports.length === 1
        ? `从 Dockerfile 识别到 ${ports[0]}，请确认程序实际监听此端口。`
        : '没有识别到唯一端口。查看程序启动配置，例如 uvicorn 的 --port；以业务实际监听端口为准。';
      $('wizardDockerEnvironment').replaceChildren();
      $('wizardDockerEnvironmentEmpty').hidden = false;
      (choice?.environment || detection?.docker?.environment || []).forEach(item => addEnvironment(item.name, item.required, item.required));
    }
    const build = visible && dockerMode === 'dockerfile';
    $('wizardDockerFile').closest('label').hidden = Boolean(dockerChoices[Number($('wizardDockerChoice').value)]);
    $('wizardDockerFields').hidden = !visible;
    $('wizardDockerBuildFields').hidden = !build;
    $('wizardDockerFile').required = visible;
    $('wizardDockerContainerPort').required = build;
    $('wizardDockerPublishedPort').required = build;
    $('wizardDockerVolumesField').hidden = !build || !$('wizardDockerPersist').checked;
    $('wizardDockerVolumes').required = build && $('wizardDockerPersist').checked;
    $('wizardDockerEnvironment').querySelectorAll('[data-env-value]').forEach(input => { input.required = visible && input.closest('.wizard-env-row').dataset.required === 'true'; });
  }
  function selectEntry() {
    const selected = entryChoices[Number($('wizardEntryChoice').value)];
    if ($('wizardEntryChoice').value !== '' && selected) {
      $('wizardEntryKind').value = ['fastapi', 'python'].includes(selected.kind) ? selected.kind : '';
      $('wizardEntryPath').value = selected.path;
      $('wizardEntryObject').value = selected.object || '';
    }
    runtimeFields();
  }
  function runtimeFields() {
    if (!active) return;
    const template = $('projectTemplateInput').value;
    const service = ['python', 'go', 'java'].includes(template);
    dockerFields();
    if (entryTemplate !== template) {
      entryTemplate = template;
      const candidate = detection?.candidates?.find(item => item.template === template);
      entryChoices = candidate?.entries || [];
      if (!entryChoices.length && candidate?.entry && template === 'python') {
        const [module, object] = candidate.entry.split(':');
        entryChoices = [{kind: 'fastapi', path: module.replaceAll('.', '/') + '.py', object}];
      }
      $('wizardEntryChoice').innerHTML = '<option value="">手动填写 / 选择入口</option>' + entryChoices.map((entry, index) =>
        `<option value="${index}">${escapeHtml(entry.path)}${entry.object ? ` · ${escapeHtml(entry.object)}` : ''} (${escapeHtml(entry.kind)})</option>`).join('');
      $('wizardEntryKind').value = '';
      $('wizardEntryPath').value = '';
      $('wizardEntryObject').value = '';
      $('wizardCustomCommand').checked = false;
      if (entryChoices.length === 1) {
        $('wizardEntryChoice').value = '0';
        const entry = entryChoices[0];
        $('wizardEntryKind').value = ['fastapi', 'python'].includes(entry.kind) ? entry.kind : '';
        $('wizardEntryPath').value = entry.path;
        $('wizardEntryObject').value = entry.object || '';
      }
    }
    const custom = $('wizardCustomCommand').checked;
    $('wizardEntryFields').hidden = !service || custom;
    $('wizardEntryChoiceField').hidden = entryChoices.length < 2;
    $('wizardPythonKindField').hidden = template !== 'python';
    $('wizardEntryOptions').hidden = template !== 'python';
    $('wizardEntryObjectField').hidden = template !== 'python' || $('wizardEntryKind').value !== 'fastapi';
    const entryKind = $('wizardEntryKind').value;
    $('wizardEntryOptionsLabel').textContent = entryKind === 'fastapi'
      ? `FastAPI · 应用对象 ${$('wizardEntryObject').value || '待填写'}（可修改）`
      : entryKind === 'python' ? '普通 Python 程序（可修改）' : '请选择运行类型';
    if (template === 'python' && (!entryKind || (entryKind === 'fastapi' && !$('wizardEntryObject').value))) $('wizardEntryOptions').open = true;
    $('wizardEntryKind').required = service && template === 'python' && !custom;
    $('wizardEntryPath').required = service && !custom;
    $('wizardEntryObject').required = !$('wizardEntryObjectField').hidden && !custom;
    $('wizardCustomCommandField').hidden = !service;
    $('wizardEntryLabel').textContent = template === 'go' ? '入口目录（仓库内路径）' : template === 'java' ? '构建生成的可执行 JAR' : '启动入口（仓库内路径）';
    $('wizardEntryPath').placeholder = template === 'go' ? '例如 cmd/server，仓库根目录填写 .' : template === 'java' ? '例如 target/app.jar' : '例如 main.py 或 app/main.py';
    $('wizardEntryHelp').textContent = template === 'go' ? '填写包含 package main 和 func main 的目录，例如 cmd/server。'
      : template === 'java' ? '填写构建后的 JAR 路径，例如 target/my-app.jar；不是 Main.java 的路径。'
      : '填写仓库内的 Python 文件路径，例如 app/main.py，不需要填写 python 或 uvicorn 命令。';
    $('projectStartCommandInput').closest('label').hidden = !service;
    $('projectServicePortInput').closest('label').hidden = !service;
    $('projectServiceNameInput').closest('label').hidden = !service;
    $('projectTemplateInput').closest('label').hidden = method() !== 'template';
    $('projectScriptInput').closest('label').hidden = method() !== 'script';
    $('projectScriptInput').required = method() === 'script';
    $('wizardCommandFields').hidden = method() !== 'commands';
    $('wizardCommandsWarning').hidden = method() !== 'commands';
    $('projectRestartStepInput').required = method() === 'commands';
    if (!custom) $('projectStartCommandInput').value = '';
    $('projectStartCommandInput').readOnly = !custom;
    $('projectStartCommandInput').required = service && custom;
    $('projectStartCommandInput').placeholder = custom ? '填写项目实际启动命令' : '根据启动入口生成，在下一步查看';
    $('projectHealthInput').placeholder = template === 'docker' && dockerMode === 'dockerfile'
      ? `http://127.0.0.1:${$('wizardDockerPublishedPort').value || '8080'}/health，可留空`
      : `http://127.0.0.1:${$('projectServicePortInput').value || '8000'}/health，可留空`;
    $('wizardRuntimeHint').textContent = template === 'docker'
      ? dockerMode === 'dockerfile' ? '应用需监听 0.0.0.0。公网访问还需放行服务器端口；数据库连接地址等变量按业务填写。数据目录留空时不额外挂载。' : '保留仓库里的 Compose 配置；已有服务器 .env 仍按 Compose 规则读取。如果原部署使用自定义 -p 项目名或多份配置，请选择自己的步骤或已有脚本。'
      : method() === 'commands' ? '填写项目实际使用的步骤。构建可以留空，启动或重启步骤必须明确；准备阶段不会执行这些命令。'
      : method() === 'script' ? '填写仓库内的脚本路径，或服务器上的绝对路径。准备时检查脚本，不改写已有内容。'
      : template === 'custom' ? '未识别到可直接使用的方案，请选择已有脚本或填写部署步骤。'
      : template === 'static' ? '静态网站需要配置构建产物发布目录；当前通用模板尚不能直接完成发布，请使用已有部署脚本。'
      : template === 'go' ? '入口目录需包含 main 包。程序监听的端口以业务配置为准。'
      : template === 'java' ? '默认 target/deploy/app.jar 会自动选取根模块构建出的唯一 JAR；多模块项目请填写实际产物路径。'
      : '入口路径相对于仓库根目录。FastAPI 应用对象对应代码中的 app = FastAPI()；普通 Python 程序的监听端口以业务配置为准。';
    $('wizardRecommendation').textContent = !method() ? '暂未找到明确的部署方案，请选择项目实际使用的方式。'
      : method() === 'script' ? '沿用已有部署脚本'
      : method() === 'commands' ? localProject?.command ? `沿用 ${localProject.name} 的 Compose 项目名与配置路径` : '使用已有的构建和重启步骤'
      : `使用${projectTemplateLabel(template)}方案`;
    $('wizardRuntimeHint').hidden = method() === 'template' && ['python', 'go', 'java'].includes(template);
    window.AppSelects?.syncAll();
    preview = '';
    $('projectConfigConfirmed').checked = false;
  }
  function methodFields(reset = false) {
    if (!active) return;
    const template = $('projectTemplateInput');
    if (method() !== 'template') {
      template.value = 'custom';
      if (reset) $('projectScriptInput').value = detection?.existing_script || '';
    } else if (reset) {
      template.value = adopting() ? 'docker' : detection?.candidates?.[0]?.template || 'python';
      applyTemplateDefaults(false);
    }
    Array.from(template.options).forEach(option => {
      option.disabled = adopting() ? option.value !== 'docker' : ['custom', 'static'].includes(option.value);
    });
    runtimeFields();
  }
  async function connect() {
    if (!$('wizardSituation').value) throw new Error('请先选择项目目前的情况。');
    if (adopting()) {
      const directory = $('wizardExistingDirectory').value.trim();
      if (!directory.startsWith('/')) throw new Error('请填写已有项目在这台服务器上的绝对目录，例如 /srv/my-app。');
      if (savedKey && directory !== $('projectWorkdirInput').value) throw new Error('已保存的项目不能在此切换代码目录，请关闭后编辑项目配置。');
      $('projectWorkdirInput').value = directory;
      delete $('projectWorkdirInput').dataset.autoValue;
    }
    const project = collectProjectForm();
    if (!project.repo.trim()) throw new Error('请填写仓库地址。');
    $('wizardConnectResult').textContent = '正在检查访问权限、分支和项目文件，最长约 60 秒…';
    const result = await postJsonBody('projects-config/inspect', {project});
    platformAdvice(result.provider);
    if (!result.ok) {
      $('wizardConnectResult').innerHTML = renderFailureAdvice(result.diagnosis);
      if (result.ssh_public_keys?.length && !project.repo.startsWith('https://')) {
        $('wizardConnectResult').innerHTML += `<details open><summary>服务器 SSH 公钥</summary><pre class="command-output">${escapeHtml(result.ssh_public_keys.join('\n'))}</pre></details>`;
      }
      throw new Error('仓库尚未连接。处理上面的提示后，点击连接并识别重试。');
    }
    detection = result;
    dockerFields(true);
    entryTemplate = '';
    $('projectBranchInput').value = result.branch || project.branch;
    $('wizardBranches').innerHTML = (result.branches || []).map(branch => `<option value="${escapeHtml(branch)}"></option>`).join('');
    if (!savedKey) {
      const base = project.repo.trim().replace(/\/$/, '').split(/[/:]/).pop().replace(/\.git$/, '');
      const key = normalizedProjectKeyFromForm({key: base});
      let unique = key, suffix = 2;
      while (lastProjects.some(item => item.key === unique)) unique = `${key}-${suffix++}`;
      $('projectKeyInput').value = unique;
      $('projectNameInput').value = base;
    }
    const candidate = result.candidates?.[0];
    const compose = dockerChoices.some(choice => choice.mode === 'compose');
    $('wizardDeployMethod').value = result.existing_script ? 'script' : adopting() ? compose ? 'template' : 'commands' : candidate ? 'template' : '';
    if ($('wizardSituation').value === 'unsure') $('wizardDeployMethod').value = '';
    $('projectTemplateInput').value = method() === 'template' ? adopting() ? 'docker' : candidate?.template || 'custom' : 'custom';
    applyTemplateDefaults(false);
    if (method() !== 'template') $('projectScriptInput').value = result.existing_script || '';
    if (adopting() && localProject && $('wizardExistingDirectory').value === (localProject.git_directory || localProject.directory)) {
      // Compose project names, config files and working directories must travel together.
      $('wizardDeployMethod').value = 'commands';
      $('projectTemplateInput').value = 'custom';
      $('projectRestartStepInput').value = localProject.command || '';
      $('projectBuildStepInput').value = '';
      $('projectScriptInput').value = '';
      result.warnings = [...(result.warnings || []), localProject.command
        ? '已保留检测到的 Compose 项目名、配置和环境文件路径。请核对原部署是否还有额外环境变量或启动参数；执行更新会重建该 Compose 项目中的服务。'
        : '未能完整确认原 Compose 配置，不能自动生成更新命令。请填写原部署步骤或沿用已有脚本。'];
    }
    $('wizardPlanOptions').open = !method();
    const facts = [...new Set((result.candidates || []).flatMap(item => item.evidence || []))];
    if (result.existing_script) facts.push(result.existing_script);
    $('wizardDetectedFacts').textContent = facts.length ? `仓库文件：${facts.join('、')}。请确认实际运行方案。`
      : candidate ? '已识别项目类型，请核对入口与运行配置。' : '没有识别到明确的部署配置。可以提供现有脚本，或填写实际部署步骤。';
    $('wizardWarnings').innerHTML = (result.warnings || []).map(message => `<p>${escapeHtml(message)}</p>`).join('');
    $('wizardWarnings').hidden = !result.warnings?.length;
    methodFields();
    window.AppSelects?.syncAll();
    show(1);
  }
  async function review() {
    if ($('wizardSituation').value === 'unsure') throw new Error('仓库检查已完成。请返回第一步，确认是首次部署还是接入已有服务，再准备项目。');
    if (method() === 'template' && ['custom', 'static'].includes($('projectTemplateInput').value)) throw new Error('该类型需要现有部署脚本或明确的部署步骤，请重新选择部署方式。');
    if (savedKey && $('projectWorkdirInput').value !== lastProjects.find(item => item.key === savedKey)?.workdir) throw new Error('已准备项目不能在此更换代码目录。');
    const invalid = Array.from($('wizardConfigure').querySelectorAll('input, select, textarea')).filter(node => !node.closest('[hidden]')).find(node => {
      node.disabled = false;
      const valid = node.checkValidity();
      node.disabled = true;
      return !valid;
    });
    if (invalid) {
      for (let parent = invalid.parentElement; parent; parent = parent.parentElement) {
        if (parent.tagName === 'DETAILS') parent.open = true;
      }
      invalid.disabled = false;
      invalid.reportValidity();
      throw new Error('请检查标出的配置项。');
    }
    $('projectKeyInput').value = normalizedProjectKeyFromForm(collectProjectForm());
    if ($('projectTemplateInput').value === 'docker') {
      const entries = environmentEntries();
      if (entries.some(entry => !entry.name || !entry.value)) throw new Error('请填写变量名称和值，不需要的变量可以移除。');
      if (new Set(entries.map(entry => entry.name)).size !== entries.length) throw new Error('环境变量名称重复，请每个变量只填写一次。');
    }
    const project = collectProjectForm();
    const result = await postJsonBody('projects-config/preview', {project});
    if (result.script) {
      $('projectScriptInput').value = result.script;
      $('projectScriptInput').dataset.autoValue = result.script;
    }
    if (result.start_command && !$('wizardCustomCommand').checked) $('projectStartCommandInput').value = result.start_command;
    preview = signature();
    const docker = project.docker_config;
    $('wizardSummary').innerHTML = [
      ['项目', project.name], ['仓库分支', project.branch], ['接入场景', adopting() ? '已有服务' : '首次部署'],
      ['部署方式', method() === 'commands' ? '自己的构建和重启步骤' : method() === 'script' ? '现有部署脚本' : projectTemplateLabel(project.template)],
      ['更新触发', project.trigger_mode === 'manual' ? '手动更新' : 'Push WebHook'],
      ['服务器目录', project.workdir],
      ...(method() === 'commands' ? [['构建步骤', project.deployment_plan.build || '跳过构建'], ['启动 / 重启步骤', project.deployment_plan.restart]] :
        [[docker?.mode ? '启动方式' : method() === 'script' ? '已有脚本' : '启动入口', docker?.mode ? docker.mode === 'dockerfile' ? '沿用 Dockerfile 的启动命令' : '沿用 Compose 的运行配置' : project.entry_kind ? `${project.entry_path}${project.entry_object && project.entry_kind === 'fastapi' ? ` · ${project.entry_object}` : ''}` : result.start_command || project.start_command || result.script || project.script]]),
      ...(docker?.mode ? [['容器配置', docker.file],
        ...(docker.mode === 'dockerfile' ? [['访问端口', `${docker.published_port} → 容器 ${docker.container_port} · ${docker.access === 'public' ? '服务器 IP 访问' : '仅本机'}`], ['数据保存', docker.volumes.join('、') || '不额外挂载']] : []),
        ['环境变量', docker.environment.join('、') || '未补充变量']] : []),
      ['健康检查', project.health_url || '未设置，部署脚本成功后仍需确认业务访问'],
    ].map(([name, value]) => `<div><dt>${escapeHtml(name)}</dt><dd>${escapeHtml(value)}</dd></div>`).join('');
    $('wizardPlanSteps').innerHTML = [...(result.plan_steps || []), ...(result.execution_steps || [])].map(value => `<li>${escapeHtml(value)}</li>`).join('');
    $('wizardFiles').innerHTML = (result.files || []).map(file => `<details><summary>${escapeHtml(file.path)} · ${file.exists ? file.managed ? '将更新面板配置' : '保留现有文件' : method() === 'script' ? '准备时检查脚本，不生成' : '将生成初版'}</summary><pre class="command-output">${escapeHtml(file.content)}</pre></details>`).join('');
    $('wizardReviewTitle').textContent = adopting() ? '确认接入，暂不更新服务' : '确认首次部署';
    $('wizardReviewHint').textContent = adopting()
      ? '接入准备不拉取、切换代码或重启服务。可以先完成接入；只有点击执行更新或启用并收到 Push WebHook 才运行部署方案。'
      : '确认后先准备文件、检查环境，通过后自动提交首次部署。检查失败会停在这里，修正后可重试。数据库、运行环境和网络权限仍需按业务准备。';
    platformAdvice();
    $('wizardProgress').replaceChildren();
    show(2);
  }
  function remember(config) {
    lastConfig = config;
    lastProjects = config.projects || [];
    savedKey = normalizedProjectKeyFromForm(collectProjectForm());
    $('projectKeyInput').value = savedKey;
    $('projectKeyInput').readOnly = true;
    $('projectWorkdirInput').readOnly = true;
    $('projectOriginalKey').value = savedKey;
    editingProjectKey = savedKey;
    selectedProjectKey = savedKey;
    $('wizardWebhookUrl').textContent = projectWebhookUrl(savedKey);
  }
  async function check() {
    const doctor = await fetchJson(`projects-config/doctor?project=${encodeURIComponent(savedKey)}`);
    const preflight = await fetchJson(`preflight?project=${encodeURIComponent(savedKey)}`);
    const checks = (doctor.checks || []).map(item => ({title: item.title, detail: item.message,
      blocked: !item.ok && item.level !== 'warn'}));
    for (const item of preflight.items || []) {
      if (item.title === 'Project is disabled') continue;
      const titles = {'Docker command was not found': '未安装 Docker', 'Docker daemon is unavailable': 'Docker 服务不可用',
        'docker-compose file was not found': '缺少 Compose 配置', 'Disk free space is low': '磁盘剩余空间不足',
        'Health URL is not configured': '尚未设置健康检查', 'Git working tree has local changes': '服务器代码目录有本地修改',
        'Deploy script is not executable': '部署脚本没有执行权限'};
      if (item.level !== 'ok') checks.push({title: titles[item.title] || item.title,
        detail: item.title === 'Health URL is not configured' ? '可在运行配置中填写健康检查地址；未填写时需自行确认业务可访问。' : item.detail,
        command: item.level === 'critical' ? item.command : '',
        blocked: item.level === 'critical'});
    }
    $('wizardProgress').innerHTML = `<p>${adopting() ? '接入检查已完成，未更新代码或重启服务。' : '项目文件已准备，尚未执行部署。'}</p>` + checks.map(item =>
      `<div class="wizard-check ${item.blocked ? 'blocked' : ''}"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.detail)}</span>${item.command ? `<pre class="command-output">${escapeHtml(item.command)}</pre>` : ''}</div>`).join('');
    return !checks.some(item => item.blocked);
  }
  async function prepare() {
    if (!$('projectConfigConfirmed').checked || preview !== signature()) throw new Error('请先确认运行配置；配置有变化时请返回上一步重新确认。');
    $('wizardProgress').textContent = adopting() ? '正在核对已有目录和准备接入，不更新业务…' : '正在保存项目、拉取代码和准备部署文件…';
    const project = {...collectProjectForm(), enabled: false};
    const config = await postJsonBody('projects-config/bootstrap', {original_key: savedKey, project,
      options: {write_service: ['python', 'go', 'java'].includes(project.template),
        ...(project.template === 'docker' ? {environment: environmentValues()} : {})}});
    remember(config);
    // The bootstrap endpoint saves before initialization; retain its key on failure for retries.
    if (!config.bootstrap?.ok) {
      $('wizardProgress').innerHTML = (config.bootstrap?.results || []).filter(item => !item.ok).map(item =>
        `<p>${escapeHtml(item.detail)}</p>${renderFailureAdvice(item.diagnosis)}`).join('');
      $('wizardFinish').hidden = false;
      throw new Error('项目准备未完成，可处理提示后重试。项目配置已保存。');
    }
    prepared = true;
    show(2);
    $('wizardReviewTitle').textContent = adopting() ? '准备完成，可接入或执行一次更新' : '准备完成，开始首次部署';
    if (!await check()) error('运行环境还有缺少项，处理后点击重新检查环境。');
    else if (!adopting()) await deploy(false);
  }
  async function task(action) {
    if (busy || !active) return;
    const version = generation;
    error(''); lock(true);
    try { await action(); }
    catch (err) { if (version === generation) error(err.message); }
    finally { if (version === generation) lock(false); }
  }
  const next = () => task(() => step === 0 ? connect() : step === 1 ? review() : prepared ? Promise.resolve() : prepare());
  $('wizardNext').addEventListener('click', next);
  $('wizardBack').addEventListener('click', () => { if (!busy) { prepared = false; show(Math.max(0, step - 1)); } });
  $('wizardFinish').addEventListener('click', () => task(async () => {
    if (adopting() && prepared) {
      if (!await check()) throw new Error('接入检查尚未通过，请处理提示后重试。');
      if (preview !== signature()) throw new Error('配置已变化，请重新确认并准备。');
      remember(await postJsonBody('projects-config/save', {original_key: savedKey, project: {...collectProjectForm(), enabled: true}}));
    }
    lock(false);
    closeProjectModal(); refresh();
  }));
  $('wizardRecheck').addEventListener('click', () => task(async () => { if (!await check()) error('运行环境仍有缺少项，请处理后重试。'); }));
  async function deploy(recheck = true) {
    if (preview !== signature()) throw new Error('配置已变化，请重新确认并准备。');
    if (recheck && !await check()) throw new Error('运行环境尚未就绪，请先处理检查结果。');
    const project = {...collectProjectForm(), enabled: true, manual_deploy_enabled: true};
    remember(await postJsonBody('projects-config/save', {original_key: savedKey, project}));
    await postJson(`redeploy?project=${encodeURIComponent(savedKey)}`);
    lock(false);
    closeProjectModal();
    setView('deploy');
    $('logs').textContent = '部署已加入队列，正在等待执行…';
  }
  $('wizardDeploy').addEventListener('click', () => task(() => deploy()));
  $('wizardDiscover').addEventListener('click', () => task(discover));
  $('wizardLocalProjects').addEventListener('click', event => {
    const button = event.target.closest('[data-local-project]');
    if (button && !busy) selectLocalProject(Number(button.dataset.localProject));
  });
  $('wizardExistingDirectory').addEventListener('input', () => {
    if (localProject) $('wizardDiscoverStatus').textContent = '目录已修改，将按你填写的目录检查。';
    localProject = null;
    $('wizardLocalProjects').querySelectorAll('button').forEach(button => {
      button.textContent = '选择'; button.setAttribute('aria-pressed', 'false');
    });
  });
  document.querySelectorAll('[data-wizard-copy]').forEach(button => button.addEventListener('click', () => task(() => copyText($(button.dataset.wizardCopy).textContent))));
  $('wizardSituation').addEventListener('change', situationFields);
  $('wizardDeployMethod').addEventListener('change', () => methodFields(true));
  $('projectTriggerInput').addEventListener('change', () => platformAdvice());
  $('projectTemplateInput').addEventListener('change', runtimeFields);
  $('wizardEntryChoice').addEventListener('change', selectEntry);
  $('wizardCustomCommand').addEventListener('change', runtimeFields);
  $('wizardEntryKind').addEventListener('change', runtimeFields);
  $('wizardDockerChoice').addEventListener('change', () => {
    const choice = dockerChoices[Number($('wizardDockerChoice').value)];
    const values = environmentValues();
    dockerMode = choice?.mode || $('wizardDockerChoice').value;
    $('wizardDockerFile').value = choice?.file || '';
    $('wizardDockerEnvironment').replaceChildren();
    $('wizardDockerEnvironmentEmpty').hidden = false;
    (choice?.environment || []).forEach(item => {
      addEnvironment(item.name, item.required, item.required);
      $('wizardDockerEnvironment').lastChild.querySelector('[data-env-value]').value = values[item.name] || '';
    });
    runtimeFields();
  });
  $('wizardDockerPersist').addEventListener('change', () => { dockerFields(); window.AppSelects?.syncAll(); });
  $('wizardDockerPublishedPort').addEventListener('input', runtimeFields);
  $('wizardDockerAddEnv').addEventListener('click', () => addEnvironment());
  $('wizardCopyWebhookUrl').addEventListener('click', () => task(() => copyFromElement('wizardWebhookUrl', 'wizardCopyWebhookUrl')));
  $('wizardCopyWebhookSecret').addEventListener('click', () => task(async () => {
    const project = lastProjects.find(item => item.key === savedKey);
    if (!project?.webhook_secret) throw new Error('尚未生成密钥，请先准备项目。');
    await copyText(project.webhook_secret);
  }));
  ['wizardEntryPath', 'wizardEntryObject'].forEach(id => $(id).addEventListener('input', () => {
    $('wizardEntryChoice').value = '';
    runtimeFields();
  }));
  ['projectWorkdirInput', 'projectServicePortInput', 'projectKeyInput'].forEach(id => $(id).addEventListener('change', runtimeFields));
  return {open, close, next, entrySettings, active: () => active, busy: () => busy};
})();
