const THREE = window.THREE;

function clamp(value) {
  return Math.min(1, Math.max(0, value));
}

function averageImageColor(image) {
  const canvas = document.createElement("canvas");
  canvas.width = 16;
  canvas.height = 16;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  context.drawImage(image, 0, 0, 16, 16);
  const pixels = context.getImageData(0, 0, 16, 16).data;
  let red = 0;
  let green = 0;
  let blue = 0;
  let count = 0;
  for (let index = 0; index < pixels.length; index += 4) {
    if (pixels[index + 3] < 128) continue;
    const lightness = (pixels[index] + pixels[index + 1] + pixels[index + 2]) / 3;
    if (lightness > 244 || lightness < 18) continue;
    red += pixels[index];
    green += pixels[index + 1];
    blue += pixels[index + 2];
    count += 1;
  }
  if (!count) return 0xb3a6f5;
  return (Math.round(red / count) << 16)
    | (Math.round(green / count) << 8)
    | Math.round(blue / count);
}

export function createBuddy(onPhotoColor) {
  const group = new THREE.Group();
  const body = new THREE.Group();
  body.position.y = 0.7;
  group.add(body);

  const material = (color, emissive = 0x000000, intensity = 0, roughness = 0.55) =>
    new THREE.MeshStandardMaterial({
      color,
      emissive,
      emissiveIntensity: intensity,
      roughness,
    });
  const sphere = (radius) => new THREE.SphereGeometry(radius, 24, 18);
  const add = (geometry, mat, position, scale, parent = body) => {
    const mesh = new THREE.Mesh(geometry, mat);
    mesh.position.set(...position);
    if (scale) mesh.scale.set(...scale);
    parent.add(mesh);
    return mesh;
  };
  const ghostMaterial = material(0xb3a6f5, 0x6d59b1, 0.22);
  const handMaterial = material(0xfaf7ff, 0x9a92d8, 0.2);
  const faceMaterial = material(0x241d39, 0x000000, 0, 0.32);

  const profile = [
    [0, 0.5], [0.55, 0.45], [0.95, 0.4], [1.07, 0.65],
    [1.1, 1.2], [1.02, 2.2], [0.75, 2.9], [0.35, 3.2], [0, 3.26],
  ].map(([radius, height]) => new THREE.Vector2(radius, height));
  const shape = new THREE.SplineCurve(profile).getPoints(48);
  const geometry = new THREE.LatheGeometry(shape, 48);
  const positions = geometry.attributes.position;
  const baseline = new Float32Array(positions.count);
  const theta = new Float32Array(positions.count);
  const radius = new Float32Array(positions.count);
  const vertexColors = new Float32Array(positions.count * 3);
  for (let index = 0; index < positions.count; index += 1) {
    const x = positions.getX(index);
    const z = positions.getZ(index);
    baseline[index] = positions.getY(index);
    theta[index] = Math.atan2(x, z);
    radius[index] = Math.hypot(x, z);
  }
  geometry.setAttribute("color", new THREE.BufferAttribute(vertexColors, 3));
  ghostMaterial.vertexColors = true;
  const ghost = new THREE.Mesh(geometry, ghostMaterial);
  body.add(ghost);

  const eyes = [];
  const cheeks = [];
  for (const side of [-1, 1]) {
    const eye = new THREE.Group();
    eye.position.set(side * 0.4, 2.2, 0.93);
    body.add(eye);
    add(sphere(0.2), faceMaterial, [0, 0, 0], [1, 1.3, 0.5], eye);
    add(sphere(0.06), material(0xffffff), [0.06, 0.14, 0.075], [1, 1, 0.3], eye);
    eyes.push(eye);
    const cheek = add(sphere(0.2), material(0xf18eaf, 0xf18eaf, 0.15), [side * 0.73, 1.82, 0.785], [0.8, 0.32, 0.12]);
    cheeks.push(cheek);
  }
  const mouth = add(sphere(0.2), material(0x4a1030), [0, 1.78, 1], [1.1, 0.14, 0.5]);
  const smile = add(
    new THREE.TorusGeometry(0.13, 0.028, 8, 20, Math.PI),
    material(0x4a1030),
    [0, 1.87, 1.05],
  );
  smile.rotation.z = Math.PI;

  const photoMaterial = new THREE.MeshBasicMaterial({
    transparent: true,
    depthWrite: false,
    side: THREE.DoubleSide,
  });
  const photo = new THREE.Mesh(new THREE.CircleGeometry(0.37, 40), photoMaterial);
  photo.position.set(0, 2.2, 1.08);
  photo.visible = false;
  body.add(photo);
  const photoRim = new THREE.Mesh(
    new THREE.TorusGeometry(0.38, 0.025, 8, 40),
    material(0xe4c395, 0x8d6c38, 0.22, 0.3),
  );
  photoRim.position.copy(photo.position);
  photoRim.visible = false;
  body.add(photoRim);

  const shadow = new THREE.Mesh(
    new THREE.CircleGeometry(1.2, 32),
    new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.19 }),
  );
  shadow.rotation.x = -Math.PI / 2;
  shadow.position.y = 0.01;
  group.add(shadow);

  const hands = [];
  for (const side of [-1, 1]) {
    const hand = new THREE.Group();
    group.add(hand);
    add(sphere(0.3), handMaterial, [0, 0, 0], [1.1, 1.15, 0.85], hand);
    const fingers = [];
    for (let finger = 0; finger < 4; finger += 1) {
      const x = (finger - 1.5) * 0.125;
      const digit = new THREE.Group();
      digit.position.set(x, 0.12, 0.02);
      hand.add(digit);
      add(sphere(0.105), handMaterial, [0, 0.12, 0], [1, 1.25, 0.9], digit);
      fingers.push(digit);
    }
    hand.position.set(side * 1.5, 1.2, 0.8);
    hands.push({ side, group: hand, fingers });
  }

  const setColor = (hex) => {
    const color = new THREE.Color(hex);
    const highlight = new THREE.Color(0xffffff).lerp(color, 0.16);
    for (let index = 0; index < positions.count; index += 1) {
      const vertex = color.clone().lerp(
        highlight,
        clamp((baseline[index] - 0.3) / 1.8),
      );
      vertexColors[index * 3] = vertex.r;
      vertexColors[index * 3 + 1] = vertex.g;
      vertexColors[index * 3 + 2] = vertex.b;
    }
    geometry.attributes.color.needsUpdate = true;
    ghostMaterial.emissive.copy(color).multiplyScalar(0.55);
    handMaterial.color.copy(highlight);
    handMaterial.emissive.copy(color).multiplyScalar(0.2);
    onPhotoColor?.(color);
  };

  const setPhotoMask = (texture) => {
    photo.material.map = texture;
    photo.material.needsUpdate = true;
    photo.visible = Boolean(texture);
    photoRim.visible = Boolean(texture);
    for (const eye of eyes) eye.visible = !texture;
    for (const cheek of cheeks) cheek.visible = !texture;
    mouth.visible = !texture;
    smile.visible = !texture;
    if (texture?.image) {
      try {
        setColor(averageImageColor(texture.image));
      } catch (error) {
        console.warn("Не удалось определить средний цвет фотографии игрока.", error);
        setColor(0xb3a6f5);
      }
    }
  };

  let state = "idle";
  let talkLevel = null;
  let mouthOpen = 0;
  let nextBlink = 2;
  let blinkAt = -1;
  const lerp = (start, end, amount) => start + (end - start) * amount;

  setColor(0xb3a6f5);
  return {
    group,
    setColor,
    setPhotoMask,
    setState(value) {
      state = value;
    },
    setTalkLevel(value) {
      talkLevel = value;
    },
    update(time) {
      const bob = 0.008 * Math.sin(time * 1.2);
      body.position.y = 0.7 + bob;
      body.rotation.z = 0.025 * Math.sin(time * 0.9);
      shadow.scale.set(1 - bob, 0.6 * (1 - bob), 1);
      if (time > nextBlink) {
        blinkAt = time;
        nextBlink = time + 2.5 + Math.random() * 3;
      }
      const blink = time - blinkAt;
      const eyeScale = blink >= 0 && blink < 0.14
        ? 1 - Math.sin((blink / 0.14) * Math.PI) * 0.92
        : 1;
      for (const eye of eyes) eye.scale.y = eyeScale;
      for (const { side, group: hand, fingers } of hands) {
        const phase = side > 0 ? 0 : 1.7;
        let x = side * 1.5;
        let y = 1.5 + 0.1 * Math.sin(time * 1.8 + phase);
        let z = 0.8;
        let rotation = -side * 0.15 + 0.05 * Math.sin(time * 1.5 + phase);
        if (state === "talk") {
          x = side * (1.5 + 0.3 * Math.sin(time * 2.3 + phase));
          y = 1.6 + 0.4 * Math.sin(time * 3 + phase);
          z = 0.9 + 0.3 * Math.sin(time * 2 + phase);
          rotation = 0.4 * Math.sin(time * 3 + phase);
        } else if (state === "wave" && side > 0) {
          x = 1.5;
          y = 2.9;
          rotation = 0.5 * Math.sin(time * 9);
        } else if (state === "clap") {
          x = side * (0.3 + 0.2 * (0.5 + 0.5 * Math.sin(time * 10)));
          y = 1.6;
          z = 1.5;
          rotation = 0;
        }
        hand.position.lerp(new THREE.Vector3(x, y + 0.7 + bob, z), 0.12);
        hand.rotation.z += (rotation - hand.rotation.z) * 0.12;
        fingers.forEach((finger, index) => {
          finger.rotation.z = (index - 1.5) * -0.09
            + (state === "talk" ? 0.12 : 0.05) * Math.sin(time * 5 + index + side);
        });
      }
      let targetMouth = talkLevel;
      if (targetMouth === null) {
        targetMouth = state === "talk" ? 0.2 + 0.8 * Math.abs(Math.sin(time * 4.5)) : 0;
      }
      mouthOpen = lerp(mouthOpen, targetMouth, 0.3);
      if (!photo.visible) {
        mouth.visible = mouthOpen > 0.06;
        smile.visible = !mouth.visible;
        mouth.scale.set(1.1 - 0.35 * mouthOpen, 0.1 + mouthOpen * 1.5, 0.5);
      }
    },
  };
}
