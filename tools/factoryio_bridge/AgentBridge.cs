// Project-owned prototype. Uses the inspected Factory I/O assembly on Unity's main thread.
// Not installed automatically. No application DLL patching or license changes.
using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using Newtonsoft.Json.Linq;
using UnityEngine;
using FactoryIO.Scripting;

namespace FactoryIOAgentBridge
{
    public static class Bootstrap
    {
        public static string Protocol() { return "factoryio-agent-file-bridge/1"; }

        public static void Install(string root, string expectedAssemblySha256)
        {
            if (!Path.IsPathRooted(root)) throw new ArgumentException("IPC root must be absolute");
            string actual = AgentBridge.Hash(typeof(Scripting).Assembly.Location);
            if (!String.Equals(actual, expectedAssemblySha256, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Factory I/O assembly hash does not match inspected version");
            GameObject owner = GameObject.FindGameObjectWithTag("Scripting");
            if (owner == null) throw new InvalidOperationException("Scripting object is not ready");
            if (owner.GetComponent<AgentBridge>() != null) throw new InvalidOperationException("Bridge already installed");
            AgentBridge bridge = owner.AddComponent<AgentBridge>();
            bridge.Initialize(Path.GetFullPath(root), actual);
        }
    }

    public sealed class AgentBridge : MonoBehaviour
    {
        private string _root, _assemblySha;
        private Scene _scene;
        private App _app;
        private EditContext _edit;
        private CameraManager _camera;
        private UI _ui;
        private JObject _pending;
        private int _issuedFrame, _stableFrames, _installedFrame;
        private float _deadline, _nextPoll;
        private string _pendingPath, _pendingSha;
        private long _lastLength;
        private string _loadError;
        private JObject _savedSnapshot;
        private PushButton _pendingButton;
        private bool _requestedPress;
        private JObject _dismissedDialog;
        private JObject _cameraFit;

        public void Initialize(string root, string assemblySha)
        {
            _root = root; _assemblySha = assemblySha;
            Directory.CreateDirectory(Path.Combine(_root, "requests"));
            Directory.CreateDirectory(Path.Combine(_root, "responses"));
            Directory.CreateDirectory(Path.Combine(_root, "images"));
            _scene = GetComponent<Scene>(); _app = GetComponent<App>();
            _edit = GetComponent<EditContext>(); _camera = GetComponent<CameraManager>();
            _ui = GetComponent<UI>();
            _installedFrame = Time.frameCount;
            _scene.OnLoadFailed += OnLoadFailed;
            WriteNew(Path.Combine(_root, "bridge-ready-" + Guid.NewGuid().ToString("N") + ".json"),
                new JObject { {"protocol", Bootstrap.Protocol()}, {"installed_at", DateTime.UtcNow.ToString("o")},
                    {"assembly_sha256", _assemblySha}, {"process_id", System.Diagnostics.Process.GetCurrentProcess().Id} });
        }

        private void OnDestroy()
        {
            if (_scene != null) _scene.OnLoadFailed -= OnLoadFailed;
        }

        private void OnLoadFailed(Scene sender, string error) { _loadError = error; }

        private void Update()
        {
            if (_root == null || Time.frameCount < _installedFrame + 3) return;
            if (_pending != null)
            {
                try { PollPending(); }
                catch (Exception e) { Complete(false, e.GetType().Name + ": " + e.Message, null); }
                return;
            }
            if (Time.realtimeSinceStartup < _nextPoll) return;
            _nextPoll = Time.realtimeSinceStartup + 0.1f;
            string[] files = Directory.GetFiles(Path.Combine(_root, "requests"), "*.json");
            Array.Sort(files, StringComparer.Ordinal);
            if (files.Length == 0) return;
            JObject request = null;
            try
            {
                if (new FileInfo(files[0]).Length > 32768) throw new InvalidOperationException("Request too large");
                request = JObject.Parse(File.ReadAllText(files[0]));
                string id = (string)request["request_id"];
                Guid parsed;
                if (!Guid.TryParseExact(id, "N", out parsed) || Path.GetFileNameWithoutExtension(files[0]) != id)
                    throw new InvalidOperationException("Invalid request ID or filename");
                File.Move(files[0], files[0] + ".processing");
                if (File.Exists(ResponsePath(id)))
                {
                    File.Delete(files[0] + ".processing");
                    return;
                }
                _pending = request;
                if ((int?)request["schema_version"] != 1) throw new InvalidOperationException("Unsupported request schema");
                string expires = (string)request["expires_at"];
                DateTime expiry;
                if (!DateTime.TryParse(expires, null, System.Globalization.DateTimeStyles.RoundtripKind, out expiry) || expiry.ToUniversalTime() < DateTime.UtcNow)
                    throw new InvalidOperationException("Request expired");
                Dispatch();
            }
            catch (Exception e)
            {
                if (_pending != null) Complete(false, e.GetType().Name + ": " + e.Message, null);
                else
                {
                    // Quarantine malformed files so they cannot block subsequent valid requests.
                    try { File.Move(files[0], files[0] + ".rejected"); } catch { }
                    UnityEngine.Debug.LogWarning("Agent bridge rejected malformed request: " + e.GetType().Name);
                }
            }
        }

        private void Dispatch()
        {
            string action = (string)_pending["action"];
            if (action == "status" || action == "prefabs")
            {
                JObject data = Status();
                if (action == "prefabs") data["prefabs"] = new JArray(_edit.Prefabs.Keys);
                Complete(true, null, data); return;
            }
            if (action == "prefab_geometry")
            {
                JObject data = new JObject { {"app_version", _app.Version}, {"assembly_sha256", _assemblySha},
                    {"coordinate_system", Coordinates()}, {"world_bounds", WorldBounds()},
                    {"bridge", BridgeIdentity()}, {"prefabs", PrefabGeometry()} };
                Complete(true, null, data); return;
            }
            if (_scene.IsLoading) throw new InvalidOperationException("Scene is loading; retry after completion");
            if (action == "save_snapshot")
            {
                JObject snapshot = SaveSnapshot();
                JObject data = Status(); data["saved_snapshot"] = snapshot;
                Complete(true, null, data); return;
            }
            if (action == "connect_modbus")
            {
                DriverManager manager = DriverManager.Instance;
                if (manager.CurrentDriver != DriverManager.Driver.ModbusTCPClient)
                    throw new InvalidOperationException("Scene must select Modbus TCP Client before connecting");
                ModbusTCPClientDriver driver = manager.ModbusTCPClientDriver;
                driver.IPAddress = "127.0.0.1"; driver.Port = 502; driver.SlaveID = 1;
                driver.AutoConnect = true; driver.Connect();
                _issuedFrame = Time.frameCount; _deadline = Time.realtimeSinceStartup + 10f;
                return;
            }
            if (action == "disconnect_modbus")
            {
                ModbusTCPClientDriver driver = DriverManager.Instance.ModbusTCPClientDriver;
                driver.AutoConnect = false; driver.Disconnect();
                _issuedFrame = Time.frameCount; _deadline = Time.realtimeSinceStartup + 10f;
                return;
            }
            if (action == "acknowledge_modbus_notice")
            {
                AcknowledgeWindowNG window = _ui.AcknowledgeWindow;
                if (!window.IsVisible || window.TitleText != Localization.Get("$DW_DRIVER_ModbusTCPClient") ||
                    window.ContentText != Localization.Get("$DW_DRIVER_Message_LostConnectionWithServer") ||
                    window.onOk != null || window.PrevWindow != null)
                    throw new InvalidOperationException("only the inspected Modbus lost-connection notice can be acknowledged");
                JObject notice = new JObject { {"title",window.TitleText}, {"content",window.ContentText},
                    {"method","AcknowledgeWindowNG.OkClicked; inspected notification; no callback"} };
                window.OkClicked(); JObject data = Status(); data["acknowledged_notice"] = notice;
                Complete(true,null,data); return;
            }
            if (action == "operator_button")
            {
                Guid key;
                if (!Guid.TryParse(RequiredString("object_key"), out key)) throw new InvalidOperationException("Invalid button object key");
                JToken pressed = _pending["pressed"];
                if (pressed == null || pressed.Type != JTokenType.Boolean) throw new InvalidOperationException("pressed must be boolean");
                _pendingButton = null;
                foreach (PushButton b in UnityEngine.Object.FindObjectsOfType<PushButton>())
                {
                    VoxelObject o = b.GetComponent<VoxelObject>();
                    if (o != null && o.Key == key && (o.PrefabName == "StartPushButton" || o.PrefabName == "StopPushButton" || o.PrefabName == "ResetPushButton"))
                    { if (_pendingButton != null) throw new InvalidOperationException("Ambiguous button key"); _pendingButton = b; }
                }
                if (_pendingButton == null) throw new InvalidOperationException("Requested operator button not found");
                // Set only this inspected mouse latch; native PushButton/OperatingMode updates produce the input.
                // No sensor/actuator values or arbitrary method/field names are exposed.
                _requestedPress = (bool)pressed;
                typeof(PushButton).GetField("_clicking", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance).SetValue(_pendingButton, _requestedPress);
                _issuedFrame = Time.frameCount; _deadline = Time.realtimeSinceStartup + 3f;
                return;
            }
            if (action == "camera")
            {
                int index = RequiredInt("index");
                if (index < 0 || index >= _camera.CameraPositions.Count) throw new InvalidOperationException("Saved camera index does not exist");
                _camera.CameraPositionIndex = index;
                Complete(true, null, Status()); return;
            }
            if (action == "camera_fit")
            {
                string view = RequiredString("view");
                Vector3 direction;
                if (view == "iso_a") direction = new Vector3(1f, 0.9f, 1f).normalized;
                else if (view == "iso_b") direction = new Vector3(-1f, 0.9f, -1f).normalized;
                else if (view == "top") direction = new Vector3(0f, 1f, 0.01f).normalized;
                else throw new InvalidOperationException("camera view must be iso_a, iso_b or top");
                Bounds bounds = SceneBounds();
                float aspect = (float)Screen.width / Screen.height;
                float verticalHalf = _camera.FOV * Mathf.Deg2Rad * 0.5f;
                float halfFov = Mathf.Min(verticalHalf, Mathf.Atan(Mathf.Tan(verticalHalf) * aspect));
                float distance = Mathf.Max(3f, bounds.extents.magnitude / Mathf.Sin(halfFov) * 1.2f);
                if (distance > 43f) throw new InvalidOperationException("scene exceeds inspected orbit-camera range; capture separate regions");
                _camera.LoadPosition(new OrbitCameraPosition("Agent review " + view, bounds.center + direction * distance, bounds.center));
                _cameraFit = new JObject { {"view", view}, {"distance_world", distance},
                    {"target_world", Vector(bounds.center)}, {"bounds_min_world", Vector(bounds.min)}, {"bounds_max_world", Vector(bounds.max)} };
                _issuedFrame = Time.frameCount; _deadline = Time.realtimeSinceStartup + 3f;
                return;
            }
            if (action == "load")
            {
                _pendingPath = Path.GetFullPath(RequiredString("scene_path"));
                _pendingSha = RequiredString("scene_sha256");
                if (!File.Exists(_pendingPath) || !String.Equals(Path.GetExtension(_pendingPath), ".factoryio", StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("Scene file not found or invalid extension");
                if (Hash(_pendingPath) != _pendingSha) throw new InvalidOperationException("Scene file hash mismatch");
                _dismissedDialog = AcknowledgeLoadFailure();
                _savedSnapshot = null;
                if (_scene.CheckSceneChanged())
                {
                    if ((bool?)_pending["save_before_load"] != true) throw new InvalidOperationException("unsaved_scene_changes");
                    _savedSnapshot = SaveSnapshot();
                    if (_scene.CheckSceneChanged()) throw new InvalidOperationException("scene remains changed after snapshot save");
                }
                _loadError = null;
                _issuedFrame = Time.frameCount;
                _deadline = Time.realtimeSinceStartup + 15f;
                _scene.Load(_pendingPath);
                return;
            }
            if (action == "capture")
            {
                if (_ui.WindowOpened) throw new InvalidOperationException("visual evidence blocked by an open native window; inspect status.ui");
                int size = RequiredInt("size");
                if (size < 1 || size > 4) throw new InvalidOperationException("Capture multiplier must be 1..4");
                _pendingPath = Path.Combine(_root, "images", (string)_pending["request_id"] + ".png");
                if (File.Exists(_pendingPath)) throw new InvalidOperationException("Capture file already exists");
                _issuedFrame = Time.frameCount; _lastLength = -1; _stableFrames = 0;
                _deadline = Time.realtimeSinceStartup + 15f;
                // Inspected Scene.CaptureScreenshot combines its folder with filename. A rooted
                // filename selects our fresh IPC image path; actual runtime behavior remains to verify.
                _scene.CaptureScreenshot(Path.ChangeExtension(_pendingPath, null), true, size);
                return;
            }
            throw new InvalidOperationException("Unsupported action; arbitrary Console execution is not exposed");
        }

        private void PollPending()
        {
            string action = (string)_pending["action"];
            if (Time.realtimeSinceStartup > _deadline) throw new TimeoutException(action + " did not complete");
            if (action == "camera_fit")
            {
                if (Time.frameCount < _issuedFrame + 10 || Time.realtimeSinceStartup < _deadline - 2.3f) return;
                JObject data = Status(); data["camera_fit"] = _cameraFit;
                Complete(true, null, data); return;
            }
            if (action == "connect_modbus")
            {
                ModbusTCPClientDriver driver = DriverManager.Instance.ModbusTCPClientDriver;
                if (Time.frameCount < _issuedFrame + 3 || !driver.Connected || driver.HasError) return;
                Complete(true, null, Status()); return;
            }
            if (action == "disconnect_modbus")
            {
                if (Time.frameCount < _issuedFrame + 3 || DriverManager.Instance.ModbusTCPClientDriver.MasterConnected) return;
                Complete(true,null,Status()); return;
            }
            if (action == "operator_button")
            {
                if (Time.frameCount < _issuedFrame + 3 || _pendingButton == null || _pendingButton.IsPressed != _requestedPress) return;
                JObject data = Status(); data["operator_button"] = new JObject { {"object_key", (string)_pending["object_key"]},
                    {"pressed", _pendingButton.IsPressed}, {"normally_closed", _pendingButton.IsNormallyClosed}, {"native_input", true} };
                Complete(true, null, data); _pendingButton = null; return;
            }
            if (action == "load")
            {
                if (_loadError != null) throw new InvalidOperationException("Scene load failed: " + _loadError);
                if (Time.frameCount < _issuedFrame + 2 || _scene.IsLoading) return;
                if (!_scene.CurrentSceneInfo.HasValue || !String.Equals(Path.GetFullPath(_scene.CurrentSceneInfo.Value.Path), _pendingPath, StringComparison.OrdinalIgnoreCase)) return;
                if (Hash(_pendingPath) != _pendingSha) throw new InvalidOperationException("Scene file changed during load");
                JObject data = Status(); data["loaded_scene_sha256"] = _pendingSha;
                data["saved_snapshot"] = _savedSnapshot;
                data["acknowledged_load_failure"] = _dismissedDialog;
                data["completion_method"] = "Scene.Load -> deferred ExecuteLoad -> CurrentSceneInfo; settled frames";
                Complete(true, null, data);
            }
            else if (action == "capture")
            {
                if (Time.frameCount < _issuedFrame + 2 || !File.Exists(_pendingPath)) return;
                byte[] bytes;
                try { bytes = File.ReadAllBytes(_pendingPath); } catch (IOException) { return; }
                if (!CompletePng(bytes)) return;
                _stableFrames = bytes.LongLength == _lastLength ? _stableFrames + 1 : 0;
                _lastLength = bytes.LongLength;
                if (_stableFrames < 2) return;
                JObject data = Status(); data["image_path"] = _pendingPath; data["image_sha256"] = Hash(_pendingPath);
                data["image_bytes"] = bytes.LongLength;
                Complete(true, null, data);
            }
        }

        private JObject Status()
        {
            JArray objects = new JArray();
            Dictionary<Guid, VoxelObject> native = new Dictionary<Guid, VoxelObject>();
            foreach (VoxelObject n in UnityEngine.Object.FindObjectsOfType<VoxelObject>())
                if (n.Key != Guid.Empty) native[n.Key] = n;
            foreach (VoxelObjectProxy o in _edit.GetAllVoxelObjectsProxies())
            {
                VoxelObject n;
                native.TryGetValue(o.Key, out n);
                objects.Add(new JObject { {"key", o.Key.ToString()}, {"prefab", o.PrefabName},
                    {"position_grid", new JArray(o.Position.x, o.Position.y, o.Position.z)},
                    {"rotation", new JArray(o.Rotation.x, o.Rotation.y, o.Rotation.z, o.Rotation.w)},
                    {"fine_position", o.FinePosition}, {"fine_rotation", o.FineRotation},
                    {"bounds_world", n == null ? null : new JObject { {"min", Vector(n.Bounds.min)}, {"max", Vector(n.Bounds.max)} }},
                    {"component_geometry", n == null ? null : ComponentGeometry(n.gameObject, false)},
                    {"native_registered", n == null ? JValue.CreateNull() : new JValue(n.IsRegistered)} });
            }
            ModbusTCPClientDriver modbus = DriverManager.Instance.ModbusTCPClientDriver;
            AcknowledgeWindowNG acknowledge = _ui.AcknowledgeWindow;
            return new JObject { {"assembly_sha256", _assemblySha}, {"app_version", _app.Version},
                {"coordinate_system", Coordinates()},
                {"ui", new JObject { {"window_opened", _ui.WindowOpened},
                    {"acknowledge_visible", acknowledge.IsVisible},
                    {"acknowledge_title", acknowledge.IsVisible ? acknowledge.TitleText : null},
                    {"acknowledge_content", acknowledge.IsVisible ? acknowledge.ContentText : null} }},
                {"driver", DriverManager.Instance.CurrentDriver.ToString()},
                {"modbus", new JObject { {"connected", modbus.Connected && !modbus.HasError},
                    {"socket_connected",modbus.MasterConnected}, {"has_error",modbus.HasError}, {"host", modbus.IPAddress},
                    {"port", modbus.Port}, {"unit_id", modbus.SlaveID}, {"auto_connect", modbus.AutoConnect} }},
                {"frame", Time.frameCount}, {"scene_loading", _scene.IsLoading},
                {"world_bounds", WorldBounds()}, {"bridge", BridgeIdentity()},
                {"scene_path", _scene.CurrentSceneInfo.HasValue ? _scene.CurrentSceneInfo.Value.Path : null},
                {"camera_index", _camera.CameraPositionIndex}, {"saved_camera_count", _camera.CameraPositions.Count},
                {"objects", objects}, {"item_count", _scene.CurrentItemCount},
                {"scene_changed", _scene.CheckSceneChanged()} };
        }

        private static JArray Vector(Vector3 v) { return new JArray(v.x, v.y, v.z); }

        private static JObject BridgeIdentity()
        {
            return new JObject { {"protocol", Bootstrap.Protocol()}, {"geometry_version", 2},
                {"bridge_sha256", Hash(typeof(AgentBridge).Assembly.Location)} };
        }

        private JObject WorldBounds()
        {
            Vector3Int dims = _edit.World.Dimensions;
            return new JObject { {"min", new JArray(0, 0, 0)},
                {"max", new JArray(dims.x * 0.125f, dims.y * 0.125f, dims.z * 0.125f)},
                {"grid_dimensions", new JArray(dims.x, dims.y, dims.z)},
                {"evidence", "VoxelWorld.Dimensions; VoxelWorld.Encompasses; Voxel.GetVoxelCenter"} };
        }

        private static JObject Coordinates()
        {
            return new JObject { {"grid_metres", 0.125}, {"axis_order", new JArray("X", "Y", "Z")},
                {"vertical_axis", "Y"}, {"horizontal_axes", new JArray("X", "Z")},
                {"pivot_world_formula", "(position_grid + [0.5,0.5,0.5]) * 0.125"},
                {"evidence", "Voxel.GetVoxelCenter; VoxelObject.UpdateBoundsAndOutOfWorld"} };
        }

        private static JObject ComponentGeometry(GameObject root, bool local)
        {
            JObject data = new JObject();
            Emitter emitter = root.GetComponent<Emitter>();
            if (emitter != null && emitter.emitOrigin != null)
                data["emitter"] = new JObject { {"origin", Vector(local ? root.transform.InverseTransformPoint(emitter.emitOrigin.position) : emitter.emitOrigin.position)} };
            Sensor sensor = root.GetComponent<Sensor>();
            if (sensor != null && sensor.rayOrigin != null)
                data["sensor"] = new JObject {
                    {"origin", Vector(local ? root.transform.InverseTransformPoint(sensor.rayOrigin.position) : sensor.rayOrigin.position)},
                    {"direction", Vector(local ? root.transform.InverseTransformDirection(sensor.rayOrigin.right) : sensor.rayOrigin.right)},
                    {"range_metres", local ? sensor.defaultRangeValue : sensor.Range} };
            RollerConveyor conveyor = root.GetComponent<RollerConveyor>();
            if (conveyor != null && conveyor.rollsTransform != null)
            {
                JObject item = new JObject { {"direction", Vector(local ? root.transform.InverseTransformDirection(conveyor.rollsTransform.forward) : conveyor.rollsTransform.forward)},
                    {"default_speed_metres_per_second", conveyor.DefaultSpeed} };
                if (conveyor.rollsRenderer != null)
                {
                    Bounds bounds = conveyor.rollsRenderer.bounds;
                    Vector3 min = bounds.min, max = bounds.max;
                    if (local) { min = root.transform.InverseTransformPoint(min); max = root.transform.InverseTransformPoint(max); }
                    item["surface_bounds"] = new JObject { {"min", Vector(Vector3.Min(min, max))}, {"max", Vector(Vector3.Max(min, max))} };
                }
                data["conveyor"] = item;
            }
            BeltConveyor beltConveyor = root.GetComponent<BeltConveyor>();
            if (beltConveyor != null && beltConveyor.belt != null && beltConveyor.beltRenderer != null)
            {
                // BtLinearConveyorBelt.InternalTick uses transform.forward * speed.
                // Read that physics component, rather than guessing from the prefab's long axis.
                Transform motion = beltConveyor.belt.transform;
                Bounds bounds = beltConveyor.beltRenderer.bounds;
                Vector3 min = bounds.min, max = bounds.max;
                if (local) { min = root.transform.InverseTransformPoint(min); max = root.transform.InverseTransformPoint(max); }
                data["conveyor"] = new JObject {
                    {"direction", Vector(local ? root.transform.InverseTransformDirection(motion.forward) : motion.forward)},
                    {"default_speed_metres_per_second", beltConveyor.DefaultSpeed},
                    {"surface_bounds", new JObject { {"min", Vector(Vector3.Min(min, max))}, {"max", Vector(Vector3.Max(min, max))} }},
                    {"evidence", "BeltConveyor.beltRenderer; BtLinearConveyorBelt.InternalTick(transform.forward * speed)"} };
            }
            PushButton button = root.GetComponent<PushButton>();
            if (button != null && !local)
                data["button"] = new JObject { {"normally_closed", button.IsNormallyClosed},
                    {"alternate_mode", button.AlternateMode},
                    {"pressed", button.IsPressed}, {"evidence", "PushButtonMomentary.Update"} };
            return data;
        }

        private JObject PrefabGeometry()
        {
            JObject result = new JObject();
            System.Reflection.FieldInfo field = typeof(VoxelObject).GetField("localVoxels", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance);
            if (field == null) throw new InvalidOperationException("inspected localVoxels field is missing");
            foreach (KeyValuePair<string, GameObject> entry in _edit.Prefabs)
            {
                VoxelObject voxel = entry.Value.GetComponent<VoxelObject>();
                if (voxel == null) continue;
                Vector3Int[] cells = (Vector3Int[])field.GetValue(voxel);
                if (cells == null || cells.Length == 0) continue;
                Vector3Int min = cells[0], max = cells[0];
                foreach (Vector3Int cell in cells) { min = Vector3Int.Min(min, cell); max = Vector3Int.Max(max, cell); }
                result[entry.Key] = new JObject {
                    {"bounds_local_metres", new JObject { {"min", Vector((Vector3)min * 0.125f - Vector3.one * 0.0625f)},
                                                           {"max", Vector((Vector3)max * 0.125f + Vector3.one * 0.0625f)} }},
                    {"component_geometry_local", ComponentGeometry(entry.Value, true)},
                    {"evidence", "serialized VoxelObject.localVoxels and native component transforms"} };
            }
            return result;
        }

        private Bounds SceneBounds()
        {
            HashSet<Guid> keys = new HashSet<Guid>();
            foreach (VoxelObjectProxy o in _edit.GetAllVoxelObjectsProxies()) keys.Add(o.Key);
            Bounds bounds = new Bounds(); bool found = false;
            foreach (VoxelObject o in UnityEngine.Object.FindObjectsOfType<VoxelObject>())
            {
                if (!keys.Contains(o.Key)) continue;
                if (!found) { bounds = o.Bounds; found = true; }
                else bounds.Encapsulate(o.Bounds);
            }
            if (!found) throw new InvalidOperationException("no current scene objects to frame");
            return bounds;
        }

        private JObject AcknowledgeLoadFailure()
        {
            // This inspected dialog only acknowledges a failed load. Save/cancel, authentication,
            // licensing and every other native dialog remain outside this allowlist.
            AcknowledgeWindowNG window = _ui.AcknowledgeWindow;
            if (!window.IsVisible) return null;
            if (window.TitleText != Localization.Get("$SCENE_LoadFailed_AW_TITLE") ||
                window.onOk != null || window.PrevWindow != null)
                throw new InvalidOperationException("unrecognized native dialog; cannot acknowledge it during scene load");
            JObject receipt = new JObject { {"title", window.TitleText}, {"content", window.ContentText},
                {"method", "AcknowledgeWindowNG.OkClicked; known load-failure title; no callback"},
                {"acknowledged_at", DateTime.UtcNow.ToString("o")} };
            window.OkClicked();
            if (window.IsVisible) throw new InvalidOperationException("load-failure acknowledgement did not close");
            return receipt;
        }

        private JObject SaveSnapshot()
        {
            // Native saving switches to Edit context. Callers treat this as a test boundary.
            string directory = Path.Combine(_root, "snapshots");
            Directory.CreateDirectory(directory);
            string name = "snapshot-" + (string)_pending["request_id"];
            string destination = Path.Combine(directory, name + "." + _app.FileExtension);
            if (File.Exists(destination)) throw new InvalidOperationException("snapshot already exists");
            string previousFolder = _scene.MyScenes.Path;
            try
            {
                _scene.MyScenes.Path = directory;
                if (!_scene.Save(name, "Agent snapshot before scene replacement", SavedScene.Type.MySavedScene))
                    throw new InvalidOperationException("native scene save was not permitted");
                if (!_scene.CurrentSceneInfo.HasValue ||
                    !String.Equals(Path.GetFullPath(_scene.CurrentSceneInfo.Value.Path), Path.GetFullPath(destination), StringComparison.OrdinalIgnoreCase) ||
                    !File.Exists(destination) || new FileInfo(destination).Length == 0)
                    throw new InvalidOperationException("native snapshot save not observed");
                return new JObject { {"path", destination}, {"sha256", Hash(destination)},
                    {"saved_at", DateTime.UtcNow.ToString("o")}, {"native_save", true},
                    {"switches_to_edit", true} };
            }
            finally { _scene.MyScenes.Path = previousFolder; }
        }

        private string RequiredString(string key)
        {
            JToken value = _pending[key];
            if (value == null || value.Type != JTokenType.String || String.IsNullOrEmpty((string)value))
                throw new InvalidOperationException("Missing string: " + key);
            return (string)value;
        }

        private int RequiredInt(string key)
        {
            JToken value = _pending[key];
            if (value == null || value.Type != JTokenType.Integer) throw new InvalidOperationException("Missing integer: " + key);
            return (int)value;
        }

        private string ResponsePath(string id) { return Path.Combine(_root, "responses", id + ".json"); }

        private void Complete(bool ok, string error, JObject data)
        {
            string id = (string)_pending["request_id"];
            JObject response = new JObject { {"schema_version", 1}, {"protocol", Bootstrap.Protocol()},
                {"request_id", id}, {"action", (string)_pending["action"]}, {"ok", ok},
                {"completed_at", DateTime.UtcNow.ToString("o")}, {"error", error}, {"data", data} };
            WriteNew(ResponsePath(id), response);
            File.Delete(Path.Combine(_root, "requests", id + ".json.processing"));
            _pending = null;
        }

        private static bool CompletePng(byte[] b)
        {
            byte[] sig = new byte[] {137, 80, 78, 71, 13, 10, 26, 10};
            if (b.Length < 45) return false;
            for (int i = 0; i < sig.Length; i++) if (b[i] != sig[i]) return false;
            int end = b.Length - 12;
            return b[end] == 0 && b[end + 1] == 0 && b[end + 2] == 0 && b[end + 3] == 0 &&
                b[end + 4] == 73 && b[end + 5] == 69 && b[end + 6] == 78 && b[end + 7] == 68;
        }

        private static void WriteNew(string path, JObject value)
        {
            string tmp = path + ".tmp-" + Guid.NewGuid().ToString("N");
            File.WriteAllText(tmp, value.ToString(), new System.Text.UTF8Encoding(false));
            File.Move(tmp, path);
        }

        public static string Hash(string path)
        {
            using (SHA256 h = SHA256.Create())
            using (FileStream f = File.OpenRead(path))
                return BitConverter.ToString(h.ComputeHash(f)).Replace("-", "").ToLowerInvariant();
        }
    }
}
