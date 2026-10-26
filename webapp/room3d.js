import { createBuddy } from "./buddy3d.js";

const THREE = window.THREE;
const SUIT_LABELS = {
  clubs: "♣",
  diamonds: "♦",
  hearts: "♥",
  spades: "♠",
};
const RANK_LABELS = { 11: "В", 12: "Д", 13: "К", 14: "Т" };
const CARD_WIDTH = 0.46;
const CARD_HEIGHT = 0.66;

function useSrgb(texture) {
  if ("colorSpace" in texture) texture.colorSpace = THREE.SRGBColorSpace;
  else texture.encoding = THREE.sRGBEncoding;
}

function cardKey(card) {
  return `${card.suit}:${card.rank}`;
}

function roundedRect(context, x, y, width, height, radius) {
  const right = x + width;
  const bottom = y + height;
  context.beginPath();
  context.moveTo(x + radius, y);
  context.lineTo(right - radius, y);
  context.arcTo(right, y, right, y + radius, radius);
  context.lineTo(right, bottom - radius);
  context.arcTo(right, bottom, right - radius, bottom, radius);
  context.lineTo(x + radius, bottom);
  context.arcTo(x, bottom, x, bottom - radius, radius);
  context.lineTo(x, y + radius);
  context.arcTo(x, y, x + radius, y, radius);
  context.closePath();
}

function makeCardTexture(card, back = false) {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 358;
  const context = canvas.getContext("2d");
  context.fillStyle = back ? "#252033" : "#fffaf0";
  roundedRect(context, 4, 4, 248, 350, 22);
  context.fill();
  context.strokeStyle = back ? "#d8bc8b" : "#c3a070";
  context.lineWidth = 7;
  context.stroke();
  roundedRect(context, 16, 16, 224, 326, 16);
  context.strokeStyle = back ? "#d8bc8b99" : "#d1b58e";
  context.lineWidth = 2;
  context.stroke();
  if (back) {
    context.save();
    roundedRect(context, 22, 22, 212, 314, 12);
    context.clip();
    context.strokeStyle = "#d8bc8b66";
    context.lineWidth = 3;
    for (let x = -350; x < 500; x += 28) {
      context.beginPath();
      context.moveTo(x, 20);
      context.lineTo(x + 330, 340);
      context.moveTo(x, 340);
      context.lineTo(x + 330, 20);
      context.stroke();
    }
    context.restore();
    context.beginPath();
    context.arc(128, 179, 60, 0, Math.PI * 2);
    context.strokeStyle = "#e4c395";
    context.lineWidth = 4;
    context.stroke();
    context.fillStyle = "#e4c395";
    context.font = "bold 62px Georgia";
    context.textAlign = "center";
    context.fillText("Ю", 128, 200);
  } else {
    const red = card.suit === "hearts" || card.suit === "diamonds";
    const ink = red ? "#a94859" : "#292633";
    const rank = RANK_LABELS[card.rank] || String(card.rank);
    const suit = SUIT_LABELS[card.suit] || "?";
    context.fillStyle = ink;
    context.textAlign = "left";
    context.font = "bold 45px Georgia";
    context.fillText(rank, 27, 68);
    context.font = "39px Georgia";
    context.fillText(suit, 27, 111);
    context.textAlign = "center";
    context.font = "112px Georgia";
    context.fillText(suit, 128, 218);
    context.save();
    context.translate(256, 358);
    context.rotate(Math.PI);
    context.textAlign = "left";
    context.font = "bold 45px Georgia";
    context.fillText(rank, 27, 68);
    context.font = "39px Georgia";
    context.fillText(suit, 27, 111);
    context.restore();
  }
  const texture = new THREE.CanvasTexture(canvas);
  useSrgb(texture);
  texture.anisotropy = 4;
  return texture;
}

function makeCard(card, back = false) {
  const group = new THREE.Group();
  const geometry = new THREE.PlaneGeometry(CARD_WIDTH, CARD_HEIGHT);
  const frontMaterial = new THREE.MeshBasicMaterial({
    map: makeCardTexture(card, back),
    side: THREE.DoubleSide,
    toneMapped: false,
  });
  const front = new THREE.Mesh(geometry, frontMaterial);
  front.position.z = 0.002;
  const reverse = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
    map: makeCardTexture(card, true),
    side: THREE.DoubleSide,
    toneMapped: false,
  }));
  reverse.rotation.y = Math.PI;
  reverse.position.z = -0.002;
  group.add(front, reverse);
  return group;
}

function makeTextTexture(text, color = "#f5f1ff") {
  const canvas = document.createElement("canvas");
  canvas.width = 512;
  canvas.height = 128;
  const context = canvas.getContext("2d");
  context.fillStyle = "rgba(18, 17, 24, 0.78)";
  roundedRect(context, 8, 8, 496, 112, 28);
  context.fill();
  context.strokeStyle = "rgba(228, 195, 149, 0.62)";
  context.lineWidth = 4;
  context.stroke();
  context.fillStyle = color;
  context.font = "600 43px system-ui, sans-serif";
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillText(text.slice(0, 22), 256, 64);
  const texture = new THREE.CanvasTexture(canvas);
  useSrgb(texture);
  return texture;
}

function makeTextSprite(text, color = "#f5f1ff") {
  const texture = makeTextTexture(text, color);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
    map: texture,
    transparent: true,
    depthTest: false,
  }));
  sprite.scale.set(1.24, 0.31, 1);
  sprite.renderOrder = 4;
  return sprite;
}

function profileColor(photoUrl, userId) {
  let hash = Number(userId) >>> 0;
  for (const character of photoUrl || "") {
    hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  }
  return new THREE.Color().setHSL((hash % 360) / 360, 0.48, 0.68);
}

function createRoomModel(scene) {
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(60, 60),
    new THREE.MeshStandardMaterial({ color: 0x393640, roughness: 0.94 }),
  );
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -0.03;
  floor.receiveShadow = true;
  scene.add(floor);

  const wall = new THREE.Mesh(
    new THREE.PlaneGeometry(16, 8),
    new THREE.MeshStandardMaterial({ color: 0x272530, roughness: 0.92 }),
  );
  wall.position.set(0, 3.8, -4);
  scene.add(wall);

  const wallRail = new THREE.Mesh(
    new THREE.BoxGeometry(15.8, 0.14, 0.18),
    new THREE.MeshStandardMaterial({ color: 0x71505a, roughness: 0.68 }),
  );
  wallRail.position.set(0, 0.6, -3.88);
  scene.add(wallRail);

  const tableLeg = new THREE.Mesh(
    new THREE.CylinderGeometry(0.42, 0.62, 0.82, 40),
    new THREE.MeshStandardMaterial({ color: 0x4e353a, roughness: 0.55 }),
  );
  tableLeg.position.y = 0.4;
  tableLeg.castShadow = true;
  scene.add(tableLeg);

  const tableEdge = new THREE.Mesh(
    new THREE.CylinderGeometry(1.88, 1.88, 0.2, 64),
    new THREE.MeshStandardMaterial({
      color: 0x855c55,
      roughness: 0.58,
      metalness: 0.08,
    }),
  );
  tableEdge.position.y = 0.88;
  tableEdge.castShadow = true;
  tableEdge.receiveShadow = true;
  scene.add(tableEdge);

  const tabletop = new THREE.Mesh(
    new THREE.CylinderGeometry(1.8, 1.8, 0.1, 64),
    new THREE.MeshStandardMaterial({
      color: 0x433442,
      roughness: 0.88,
      metalness: 0.02,
    }),
  );
  tabletop.position.y = 1.02;
  tabletop.castShadow = true;
  tabletop.receiveShadow = true;
  scene.add(tabletop);

  const inlay = new THREE.Mesh(
    new THREE.TorusGeometry(1.66, 0.012, 6, 64),
    new THREE.MeshStandardMaterial({
      color: 0xd7b783,
      metalness: 0.56,
      roughness: 0.3,
      emissive: 0x3a2913,
    }),
  );
  inlay.rotation.x = Math.PI / 2;
  inlay.position.y = 1.075;
  scene.add(inlay);

  const centerMark = new THREE.Mesh(
    new THREE.TorusGeometry(0.43, 0.009, 6, 48),
    new THREE.MeshBasicMaterial({ color: 0xbba0c5, transparent: true, opacity: 0.6 }),
  );
  centerMark.rotation.x = Math.PI / 2;
  centerMark.position.y = 1.078;
  scene.add(centerMark);

  const ceilingLight = new THREE.PointLight(0xf3dfbd, 4.5, 16, 2);
  ceilingLight.position.set(0, 5.4, 0);
  scene.add(ceilingLight);

  const seatGeometry = new THREE.CylinderGeometry(0.43, 0.46, 0.13, 28);
  const backGeometry = new THREE.BoxGeometry(0.72, 0.72, 0.13);
  const chairMaterial = new THREE.MeshStandardMaterial({
    color: 0x4b3c48,
    roughness: 0.83,
  });
  const chairAccent = new THREE.MeshStandardMaterial({
    color: 0x957982,
    roughness: 0.75,
  });

  return {
    createSeat() {
      const seat = new THREE.Group();
      const cushion = new THREE.Mesh(seatGeometry, chairAccent);
      cushion.position.y = 0.52;
      cushion.castShadow = true;
      seat.add(cushion);
      const back = new THREE.Mesh(backGeometry, chairMaterial);
      back.position.set(0, 0.96, -0.28);
      back.castShadow = true;
      seat.add(back);
      scene.add(seat);
      return seat;
    },
  };
}

export function createRoomScene(container, handlers) {
  if (!THREE) throw new Error("Three.js не загрузился.");

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x17161e);
  scene.fog = new THREE.Fog(0x17161e, 12, 28);
  scene.add(new THREE.HemisphereLight(0xfff4e6, 0x31273c, 1.45));
  const keyLight = new THREE.DirectionalLight(0xffe8c7, 2.3);
  keyLight.position.set(-3, 7, 5);
  keyLight.castShadow = true;
  keyLight.shadow.mapSize.set(1024, 1024);
  scene.add(keyLight);
  const fillLight = new THREE.PointLight(0x987ed0, 2.6, 10, 2);
  fillLight.position.set(3, 3.8, -2);
  scene.add(fillLight);

  const camera = new THREE.PerspectiveCamera(58, 1, 0.1, 40);
  const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.7));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  if ("outputColorSpace" in renderer) renderer.outputColorSpace = THREE.SRGBColorSpace;
  else renderer.outputEncoding = THREE.sRGBEncoding;
  renderer.domElement.className = "room-3d-canvas";
  renderer.domElement.setAttribute("aria-label", "Игровой 3D-стол");
  container.prepend(renderer.domElement);
  const roomModel = createRoomModel(scene);

  const seats = new Map();
  const cards = new Map();
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  const clock = new THREE.Clock();
  const target = new THREE.Vector3(0, 0.88, 0);
  let room;
  let roomSignature = "";
  let drag = null;
  let orbit = null;
  let leanTimer = 0;
  let leaning = false;
  let leanAmount = 0;
  let destroyed = false;
  let paused = false;
  let raf = 0;

  function resize() {
    const width = Math.max(1, container.clientWidth);
    const height = Math.max(1, container.clientHeight);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.fov = width / height < 0.78 ? 66 : 58;
    camera.updateProjectionMatrix();
  }

  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(container);
  resize();

  function positionCamera() {
    const normal = new THREE.Vector3(0, 1.62, 2.2);
    const close = new THREE.Vector3(0, 1.4, 1.9);
    leanAmount += ((leaning ? 1 : 0) - leanAmount) * 0.12;
    camera.position.lerpVectors(normal, close, leanAmount);
    camera.lookAt(0, 0.96 - leanAmount * 0.12, 0);
  }

  function createSeat(player) {
    const seat = roomModel.createSeat();
    const buddy = createBuddy();
    buddy.group.scale.setScalar(0.4);
    scene.add(buddy.group);
    const label = makeTextSprite(player.display_name || "Игрок");
    scene.add(label);
    const state = {
      player,
      seat,
      buddy,
      label,
      handKeys: [],
      color: profileColor(player.photo_url, player.user_id),
    };
    buddy.setColor(state.color);
    if (player.photo_url) {
      const loader = new THREE.TextureLoader();
      loader.setCrossOrigin("anonymous");
      loader.load(
        player.photo_url,
        (texture) => {
          if (!destroyed && seats.get(String(player.user_id)) === state) {
            useSrgb(texture);
            texture.needsUpdate = true;
            buddy.setPhotoMask(texture);
          } else {
            texture.dispose();
          }
        },
        undefined,
        (error) => {
          console.warn("Не удалось загрузить фотографию игрока для 3D-маски.", error);
        },
      );
    }
    seats.set(String(player.user_id), state);
    return state;
  }

  function disposeCard(key) {
    const mesh = cards.get(key);
    if (!mesh) return;
    scene.remove(mesh);
    mesh.traverse((object) => {
      if (!object.isMesh) return;
      object.geometry.dispose();
      if (Array.isArray(object.material)) {
        object.material.forEach((material) => {
          material.map?.dispose();
          material.dispose();
        });
      } else {
        object.material.map?.dispose();
        object.material.dispose();
      }
    });
    cards.delete(key);
  }

  function clearSeat(userId) {
    const seat = seats.get(String(userId));
    if (!seat) return;
    scene.remove(seat.seat, seat.buddy.group, seat.label);
    seat.buddy.group.traverse((object) => {
      if (object.isMesh) object.geometry.dispose();
    });
    seat.buddy.group.traverse((object) => {
      if (object.isMesh) {
        const disposeMaterial = (material) => {
          material.map?.dispose();
          material.dispose();
        };
        if (Array.isArray(object.material)) object.material.forEach(disposeMaterial);
        else disposeMaterial(object.material);
      }
    });
    seat.label.material.map?.dispose();
    seat.label.material.dispose();
    seats.delete(String(userId));
  }

  function getCard(key, card, back = false) {
    let mesh = cards.get(key);
    if (!mesh) {
      mesh = makeCard(card || { suit: "spades", rank: 6 }, back);
      mesh.userData.cardKey = key;
      scene.add(mesh);
      cards.set(key, mesh);
    }
    return mesh;
  }

  function setCardAction(mesh, action) {
    mesh.userData.cardAction = action;
    mesh.children.forEach((child) => {
      child.userData.cardAction = action;
    });
  }

  function update(roomData, selectedTarget, selectedHandIndex) {
    room = roomData;
    const game = room.game;
    const signature = JSON.stringify({
      status: room.status,
      players: room.players.map((player) => [
        player.user_id,
        player.display_name,
        player.photo_url,
        player.cards_count,
        player.has_left,
      ]),
      stage: game?.stage,
      turn: game?.current_actor_id,
      deck: game?.deck_count,
      hand: game?.your_hand,
      table: game?.table,
      selectedTarget,
      selectedHandIndex,
    });
    if (signature === roomSignature) return;
    roomSignature = signature;

    const players = game?.players || room.players;
    const playerIds = new Set(players.map((player) => String(player.user_id)));
    for (const id of seats.keys()) {
      if (!playerIds.has(id)) clearSeat(id);
    }

    const ownIndex = Math.max(0, players.findIndex(
      (player) => Number(player.user_id) === Number(window.telegramUserId),
    ));
    const seatCount = Math.max(1, players.length);
    players.forEach((player, index) => {
      const id = String(player.user_id);
      const seat = seats.get(id) || createSeat(player);
      seat.player = player;
      const isLocalPlayer = Number(player.user_id) === Number(window.telegramUserId);
      seat.seat.visible = !isLocalPlayer;
      seat.buddy.group.visible = !isLocalPlayer;
      seat.label.visible = !isLocalPlayer;
      seat.buddy.setState(
        player.user_id === game?.current_actor_id ? "talk" : "idle",
      );
      const offset = index - ownIndex;
      const angle = offset * (Math.PI * 2 / seatCount);
      const x = Math.sin(angle) * 2.45;
      const z = Math.cos(angle) * 2.45;
      const facing = angle + Math.PI;
      seat.seat.position.set(x, 0, z);
      seat.seat.rotation.y = facing;
      seat.buddy.group.position.set(x, 0.23, z);
      seat.buddy.group.rotation.y = facing;
      seat.label.position.set(x, 2.1, z);
      seat.label.material.map?.dispose();
      seat.label.material.map = makeTextTexture(
        `${player.display_name} · ${player.cards_count ?? 0}`,
      );
      seat.label.material.needsUpdate = true;
    });

    const activeKeys = new Set();
    const hand = game?.your_hand || [];
    const ownAngle = 0;
    const ownCenterX = Math.sin(ownAngle) * 1.52;
    const ownCenterZ = Math.cos(ownAngle) * 1.52;
    hand.forEach((card, index) => {
      const key = `face:${cardKey(card)}`;
      activeKeys.add(key);
      const mesh = getCard(key, card);
      const offset = index - (hand.length - 1) / 2;
      const position = new THREE.Vector3(
        ownCenterX + offset * 0.29,
        index === selectedHandIndex ? 1.68 : 1.5,
        ownCenterZ,
      );
      mesh.rotation.set(0.05, 0, -offset * 0.045);
      mesh.position.copy(position);
      mesh.scale.setScalar(1);
      setCardAction(mesh, { kind: "hand", card, index });
    });

    const table = game?.table || [];
    table.forEach((entry, index) => {
      const row = Math.floor(index / 3);
      const column = index % 3;
      const x = (column - 1) * 0.48;
      const z = row === 0 ? -0.34 : 0.28;
      for (const [type, card, dx, dz] of [
        ["attack", entry.attack, -0.1, 0],
        ["defense", entry.defense, 0.13, 0.13],
      ]) {
        if (!card) continue;
        const key = `face:${cardKey(card)}`;
        activeKeys.add(key);
        const mesh = getCard(key, card);
        const selected = selectedTarget === index && type === "attack";
        const position = new THREE.Vector3(x + dx, selected ? 1.13 : 1.09, z + dz);
        mesh.rotation.set(-Math.PI / 2, 0, type === "defense" ? 0.12 : -0.08);
        mesh.position.copy(position);
        mesh.scale.setScalar(selected ? 0.94 : 0.86);
        setCardAction(mesh, {
          kind: "table",
          tableIndex: index,
          cardType: type,
          canSelect: game?.is_your_turn
            && game?.stage === "defend"
            && entry.defense === null,
          isSelected: selectedTarget === index,
        });
      }
    });

    const deckBackCount = Math.min(Math.max(0, game?.deck_count || 0), 5);
    for (let index = 0; index < deckBackCount; index += 1) {
      const key = `deck:${index}`;
      activeKeys.add(key);
      const mesh = getCard(key, null, true);
      mesh.position.set(-1.12 + index * 0.008, 1.08 + index * 0.004, -0.23);
      mesh.rotation.set(-Math.PI / 2, 0, 0);
      mesh.scale.setScalar(0.8);
      setCardAction(mesh, null);
    }

    players.forEach((player, index) => {
      if (Number(player.user_id) === Number(window.telegramUserId)) return;
      const count = Math.min(8, Math.max(0, Number(player.cards_count) || 0));
      const angle = (index - ownIndex) * (Math.PI * 2 / seatCount);
      const centerX = Math.sin(angle) * 1.35;
      const centerZ = Math.cos(angle) * 1.35;
      for (let cardIndex = 0; cardIndex < count; cardIndex += 1) {
        const key = `back:${player.user_id}:${cardIndex}`;
        activeKeys.add(key);
        const mesh = getCard(key, null, true);
        const offset = cardIndex - (count - 1) / 2;
        mesh.position.set(
          centerX + Math.sin(angle + Math.PI / 2) * offset * 0.11,
          1.16,
          centerZ + Math.cos(angle + Math.PI / 2) * offset * 0.11,
        );
        mesh.rotation.set(0, angle + Math.PI, -offset * 0.035);
        mesh.scale.setScalar(0.46);
        setCardAction(mesh, null);
      }
    });

    for (const key of cards.keys()) {
      if (!activeKeys.has(key)) disposeCard(key);
    }
  }

  function normalizedPointer(event) {
    const bounds = renderer.domElement.getBoundingClientRect();
    pointer.set(
      ((event.clientX - bounds.left) / bounds.width) * 2 - 1,
      -((event.clientY - bounds.top) / bounds.height) * 2 + 1,
    );
  }

  function hitTest(event) {
    normalizedPointer(event);
    raycaster.setFromCamera(pointer, camera);
    const intersections = raycaster.intersectObjects(
      Array.from(cards.values()).flatMap((mesh) => mesh.children),
      false,
    );
    return intersections.find((hit) => hit.object.userData.cardAction)?.object.userData.cardAction;
  }

  function onPointerDown(event) {
    if (!room?.game) return;
    const hit = hitTest(event);
    if (hit?.kind === "hand") {
      drag = {
        hit,
        startX: event.clientX,
        startY: event.clientY,
        lastX: event.clientX,
        lastY: event.clientY,
        moved: false,
      };
      renderer.domElement.setPointerCapture(event.pointerId);
      event.preventDefault();
      return;
    }
    if (hit?.kind === "table" && hit.canSelect) {
      handlers.onTarget(hit.tableIndex);
      return;
    }
    orbit = { x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY };
    leanTimer = window.setTimeout(() => {
      leaning = true;
    }, 180);
    renderer.domElement.setPointerCapture(event.pointerId);
  }

  function onPointerMove(event) {
    if (drag) {
      const mesh = cards.get(`face:${cardKey(drag.hit.card)}`);
      if (!mesh) return;
      const dx = event.clientX - drag.startX;
      const dy = event.clientY - drag.startY;
      const unitsPerPixel = (2 * camera.position.distanceTo(target)
        * Math.tan((camera.fov * Math.PI) / 360)) / renderer.domElement.clientHeight;
      mesh.position.x += (event.clientX - drag.lastX) * unitsPerPixel;
      mesh.position.y -= (event.clientY - drag.lastY) * unitsPerPixel;
      mesh.position.y = Math.max(1.15, mesh.position.y);
      mesh.scale.setScalar(1.12);
      drag.lastX = event.clientX;
      drag.lastY = event.clientY;
      drag.moved = Math.abs(dx) + Math.abs(dy) > 8;
      event.preventDefault();
      return;
    }
    if (orbit) {
      if (Math.abs(event.clientX - orbit.startX) + Math.abs(event.clientY - orbit.startY) > 8) {
        window.clearTimeout(leanTimer);
      }
    }
  }

  function onPointerUp(event) {
    window.clearTimeout(leanTimer);
    if (drag) {
      const movementY = drag.startY - event.clientY;
      const { hit, moved } = drag;
      const mesh = cards.get(`face:${cardKey(hit.card)}`);
      drag = null;
      if (movementY > renderer.domElement.clientHeight * 0.09) {
        if (room.game.can_attack) {
          handlers.onAction("play", hit.index);
        } else if (room.game.stage === "defend" && room.game.is_your_turn) {
          if (selectedTarget === undefined || selectedTarget === null) {
            handlers.onToast("Сначала выбери карту на столе, которую нужно отбить.");
          } else {
            handlers.onAction("defend", hit.index);
          }
        } else {
          handlers.onToast("Сейчас не твой ход.");
        }
      } else if (moved && mesh) {
        mesh.position.y = 1.5;
        mesh.scale.setScalar(1);
        handlers.onSelectCard(hit.index);
      } else {
        handlers.onSelectCard(hit.index);
      }
      event.preventDefault();
    }
    orbit = null;
    leaning = false;
  }

  function onPointerCancel() {
    window.clearTimeout(leanTimer);
    if (drag) {
      const mesh = cards.get(`face:${cardKey(drag.hit.card)}`);
      if (mesh) {
        mesh.position.y = 1.5;
        mesh.scale.setScalar(1);
      }
      drag = null;
    }
    orbit = null;
    leaning = false;
  }

  const canvas = renderer.domElement;
  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("pointercancel", onPointerCancel);
  canvas.addEventListener("contextmenu", (event) => event.preventDefault());

  function animate() {
    if (destroyed || paused) {
      raf = 0;
      return;
    }
    raf = requestAnimationFrame(animate);
    const time = clock.getElapsedTime();
    seats.forEach((seat) => seat.buddy.update(time));
    positionCamera();
    renderer.render(scene, camera);
  }
  animate();

  return {
    update,
    setPaused(value) {
      paused = value;
      if (paused) {
        cancelAnimationFrame(raf);
        raf = 0;
      } else if (!raf) {
        animate();
      }
    },
    destroy() {
      destroyed = true;
      cancelAnimationFrame(raf);
      resizeObserver.disconnect();
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("pointercancel", onPointerCancel);
      window.clearTimeout(leanTimer);
      for (const key of cards.keys()) disposeCard(key);
      for (const id of seats.keys()) clearSeat(id);
      scene.traverse((object) => {
        if (!object.isMesh) return;
        object.geometry.dispose();
        if (Array.isArray(object.material)) object.material.forEach((material) => material.dispose());
        else object.material.dispose();
      });
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}
